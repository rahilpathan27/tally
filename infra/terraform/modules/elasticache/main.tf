# Redis-compatible Valkey for rate limits, nonces, idempotency fast paths and risk features.
# TLS in transit, KMS at rest, auth token from Secrets Manager, Multi-AZ automatic failover.
# Redis is never the source of truth for money (PostgreSQL is), so losing it degrades but does
# not corrupt.

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
  type = string
}

variable "secrets_kms_key_arn" {
  type = string
}

variable "node_type" {
  type    = string
  default = "cache.r7g.large"
}

variable "replicas" {
  description = "Total nodes (primary + replicas)."
  type        = number
  default     = 3
}

resource "aws_elasticache_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

resource "aws_security_group" "this" {
  name_prefix = "${var.name}-cache-"
  description = "Valkey from EKS only"
  vpc_id      = var.vpc_id
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "redis" {
  for_each                     = toset(var.allowed_security_group_ids)
  security_group_id            = aws_security_group.this.id
  referenced_security_group_id = each.key
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "Valkey from EKS"
}

# Generated here, stored only in Secrets Manager (and, unavoidably, encrypted remote state).
resource "random_password" "auth" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "auth" {
  name_prefix = "${var.name}/redis-auth-"
  kms_key_id  = var.secrets_kms_key_arn
}

resource "aws_secretsmanager_secret_version" "auth" {
  secret_id     = aws_secretsmanager_secret.auth.id
  secret_string = random_password.auth.result
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = var.name
  description                = "Tally ${var.name}"
  engine                     = "valkey"
  engine_version             = "8.0"
  node_type                  = var.node_type
  num_cache_clusters         = var.replicas
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.this.id]
  automatic_failover_enabled = var.replicas > 1
  multi_az_enabled           = var.replicas > 1
  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  auth_token                 = random_password.auth.result
  snapshot_retention_limit   = 7
  snapshot_window            = "19:00-20:00"
  maintenance_window         = "sun:22:30-sun:23:30"
  auto_minor_version_upgrade = true
}

output "primary_endpoint" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "auth_secret_arn" {
  value = aws_secretsmanager_secret.auth.arn
}
