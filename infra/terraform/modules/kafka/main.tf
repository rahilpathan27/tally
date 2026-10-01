# Amazon MSK for the payment event stream (outbox -> Kafka). TLS between clients and brokers
# and between brokers; SASL/SCRAM client authentication with credentials in Secrets Manager.
# Unauthenticated and plaintext access are disabled.

terraform {
  required_providers {
    aws    = { source = "hashicorp/aws", version = ">= 6.0" }
    random = { source = "hashicorp/random", version = ">= 3.6" }
  }
}

variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "allowed_security_group_ids" {
  type = list(string)
}

variable "kms_key_arn" {
  description = "Customer-managed key; SCRAM secrets must use a CMK (MSK requirement)."
  type        = string
}

variable "logs_kms_key_arn" {
  type = string
}

variable "instance_type" {
  type    = string
  default = "kafka.m7g.large"
}

variable "broker_count" {
  type    = number
  default = 3
}

variable "volume_gb" {
  type    = number
  default = 200
}

resource "aws_security_group" "this" {
  name_prefix = "${var.name}-msk-"
  description = "MSK SASL/SCRAM over TLS from EKS only"
  vpc_id      = var.vpc_id
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "scram" {
  for_each                     = toset(var.allowed_security_group_ids)
  security_group_id            = aws_security_group.this.id
  referenced_security_group_id = each.key
  ip_protocol                  = "tcp"
  from_port                    = 9096
  to_port                      = 9096
  description                  = "Kafka SASL_SSL from EKS"
}

resource "aws_msk_configuration" "this" {
  name           = var.name
  kafka_versions = ["3.8.x"]
  server_properties = join("\n", [
    "auto.create.topics.enable=false",
    "default.replication.factor=3",
    "min.insync.replicas=2",
    "unclean.leader.election.enable=false",
    "log.retention.hours=168",
  ])
}

resource "aws_cloudwatch_log_group" "broker" {
  name              = "/tally/${var.name}/msk"
  retention_in_days = 365
  kms_key_id        = var.logs_kms_key_arn
}

resource "aws_msk_cluster" "this" {
  cluster_name           = var.name
  kafka_version          = "3.8.x"
  number_of_broker_nodes = var.broker_count

  broker_node_group_info {
    instance_type   = var.instance_type
    client_subnets  = var.subnet_ids
    security_groups = [aws_security_group.this.id]
    storage_info {
      ebs_storage_info {
        volume_size = var.volume_gb
      }
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.this.arn
    revision = aws_msk_configuration.this.latest_revision
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = var.kms_key_arn
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  client_authentication {
    unauthenticated = false
    sasl {
      scram = true
      iam   = false
    }
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.broker.name
      }
    }
  }
}

resource "random_password" "scram" {
  length  = 48
  special = false
}

# MSK requires SCRAM secret names to start with "AmazonMSK_".
resource "aws_secretsmanager_secret" "scram" {
  name_prefix = "AmazonMSK_${var.name}-core-"
  kms_key_id  = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "scram" {
  secret_id = aws_secretsmanager_secret.scram.id
  secret_string = jsonencode({
    username = "tally-core"
    password = random_password.scram.result
  })
}

resource "aws_msk_scram_secret_association" "this" {
  cluster_arn     = aws_msk_cluster.this.arn
  secret_arn_list = [aws_secretsmanager_secret.scram.arn]
  depends_on      = [aws_secretsmanager_secret_version.scram]
}

output "bootstrap_brokers_sasl_scram" {
  value = aws_msk_cluster.this.bootstrap_brokers_sasl_scram
}

output "scram_secret_arn" {
  value = aws_secretsmanager_secret.scram.arn
}
