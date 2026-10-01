# One Tally environment: network, keys, EKS, three PostgreSQL instances, Valkey, MSK, object
# storage, registry, WAF, DNS/TLS, static-asset CDN, IAM and account security baseline.
# Environments differ only in the sizing inputs below.

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = ">= 6.0"
      configuration_aliases = [aws.us_east_1]
    }
    random = { source = "hashicorp/random", version = ">= 3.6" }
    tls    = { source = "hashicorp/tls", version = ">= 4.0" }
  }
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "zone_name" {
  type = string
}

variable "create_zone" {
  type    = bool
  default = false
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "azs" {
  type    = list(string)
  default = ["ap-south-1a", "ap-south-1b", "ap-south-1c"]
}

variable "single_nat_gateway" {
  type    = bool
  default = false
}

variable "eks_public_access_cidrs" {
  type    = list(string)
  default = []
}

variable "eks_admin_role_arns" {
  type    = list(string)
  default = []
}

variable "node_groups" {
  type = map(object({
    instance_types = list(string)
    min_size       = number
    max_size       = number
    desired_size   = number
    capacity_type  = optional(string, "ON_DEMAND")
  }))
}

variable "db_instance_classes" {
  description = "Instance class per database: general, ledger, vault."
  type        = map(string)
}

variable "db_multi_az" {
  type    = bool
  default = true
}

variable "db_dr_backup_replication" {
  type    = bool
  default = false
}

variable "cache_node_type" {
  type = string
}

variable "cache_nodes" {
  type    = number
  default = 3
}

variable "kafka_instance_type" {
  type = string
}

variable "kafka_brokers" {
  type    = number
  default = 3
}

variable "object_lock_mode" {
  type    = string
  default = "GOVERNANCE"
}

variable "github_repository" {
  type    = string
  default = "rahilpathan27/tally"
}

variable "ecr_pull_account_ids" {
  description = "Set in the account that builds images, to let other environments pull."
  type        = list(string)
  default     = []
}

variable "build_images" {
  description = "Create ECR repositories and the CI release role in this account."
  type        = bool
  default     = false
}

variable "alert_emails" {
  type    = list(string)
  default = []
}

variable "monthly_budget_usd" {
  type = number
}

variable "deletion_protection" {
  type    = bool
  default = true
}

data "aws_caller_identity" "current" {}

locals {
  name      = "tally-${var.environment}"
  namespace = "tally-${var.environment}"
  account   = data.aws_caller_identity.current.account_id
}

module "kms" {
  source   = "../kms"
  name     = local.name
  purposes = ["general-db", "ledger-db", "vault-db", "objects", "secrets", "logs", "audit", "kafka", "eks", "cache", "ecr"]
}

# Replicated RDS backups in Hyderabad need a key in that region.
data "aws_iam_policy_document" "dr_key" {
  statement {
    sid       = "AccountAdministration"
    actions   = ["kms:*"]
    resources = ["*"] # a key policy always applies to its own key
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account}:root"]
    }
  }
}

resource "aws_kms_key" "dr" {
  count                   = var.db_dr_backup_replication ? 1 : 0
  region                  = "ap-south-2"
  description             = "${local.name} DR backups"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.dr_key.json
}

module "network" {
  source               = "../network"
  name                 = local.name
  cidr                 = var.vpc_cidr
  azs                  = var.azs
  single_nat_gateway   = var.single_nat_gateway
  flow_log_kms_key_arn = module.kms.key_arns["logs"]
}

module "eks" {
  source              = "../eks"
  name                = local.name
  vpc_id              = module.network.vpc_id
  subnet_ids          = module.network.app_subnet_ids
  public_access_cidrs = var.eks_public_access_cidrs
  secrets_kms_key_arn = module.kms.key_arns["eks"]
  logs_kms_key_arn    = module.kms.key_arns["logs"]
  admin_role_arns     = var.eks_admin_role_arns
  node_groups         = var.node_groups
}

module "db" {
  source   = "../rds-postgres"
  for_each = { general = "tally", ledger = "tally_ledger", vault = "tally_vault" }

  name                       = "${local.name}-${each.key}"
  database_name              = each.value
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = module.network.data_subnet_ids
  allowed_security_group_ids = [module.eks.cluster_security_group_id]
  kms_key_arn                = module.kms.key_arns["${each.key}-db"]
  instance_class             = var.db_instance_classes[each.key]
  multi_az                   = var.db_multi_az
  deletion_protection        = var.deletion_protection
  dr_backup_replication      = var.db_dr_backup_replication
  dr_kms_key_arn             = var.db_dr_backup_replication ? aws_kms_key.dr[0].arn : null
}

module "cache" {
  source                     = "../elasticache"
  name                       = local.name
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = module.network.data_subnet_ids
  allowed_security_group_ids = [module.eks.cluster_security_group_id]
  kms_key_arn                = module.kms.key_arns["cache"]
  secrets_kms_key_arn        = module.kms.key_arns["secrets"]
  node_type                  = var.cache_node_type
  replicas                   = var.cache_nodes
}

module "kafka" {
  source                     = "../kafka"
  name                       = local.name
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = module.network.data_subnet_ids
  allowed_security_group_ids = [module.eks.cluster_security_group_id]
  kms_key_arn                = module.kms.key_arns["kafka"]
  logs_kms_key_arn           = module.kms.key_arns["logs"]
  instance_type              = var.kafka_instance_type
  broker_count               = var.kafka_brokers
}

module "objects" {
  source           = "../s3"
  bucket_name      = "${local.name}-objects-${local.account}"
  kms_key_arn      = module.kms.key_arns["objects"]
  object_lock_mode = var.object_lock_mode
}

module "ecr" {
  count            = var.build_images ? 1 : 0
  source           = "../ecr"
  kms_key_arn      = module.kms.key_arns["ecr"]
  pull_account_ids = var.ecr_pull_account_ids
}

module "iam" {
  source                      = "../iam"
  name                        = local.name
  namespace                   = local.namespace
  oidc_provider_arn           = module.eks.oidc_provider_arn
  oidc_issuer                 = module.eks.oidc_issuer
  objects_bucket_arn          = module.objects.arn
  objects_kms_key_arn         = module.kms.key_arns["objects"]
  secrets_kms_key_arn         = module.kms.key_arns["secrets"]
  secrets_prefix              = "tally/${var.environment}"
  github_repository           = var.github_repository
  ecr_repository_arns         = var.build_images ? values(module.ecr[0].repository_arns) : []
  create_github_oidc_provider = true
}

module "dns" {
  source      = "../route53-acm"
  zone_name   = var.zone_name
  create_zone = var.create_zone
  hosts       = ["api", "console", "vault", "static"]
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }
}

module "waf" {
  source           = "../waf"
  name             = local.name
  logs_kms_key_arn = module.kms.key_arns["logs"]
}

module "waf_edge" {
  source = "../waf"
  name   = "${local.name}-edge"
  scope  = "CLOUDFRONT"
  providers = {
    aws = aws.us_east_1
  }
}

module "cdn" {
  source          = "../cloudfront"
  name            = local.name
  zone_id         = module.dns.zone_id
  aliases         = ["static.${var.zone_name}"]
  origin_domain   = "console.${var.zone_name}"
  certificate_arn = module.dns.cloudfront_certificate_arn
  web_acl_arn     = module.waf_edge.arn
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }
}

module "security" {
  source            = "../security-baseline"
  name              = local.name
  audit_kms_key_arn = module.kms.key_arns["audit"]
  logs_kms_key_arn  = module.kms.key_arns["logs"]
}

module "observability" {
  source             = "../observability"
  name               = local.name
  logs_kms_key_arn   = module.kms.key_arns["logs"]
  alert_emails       = var.alert_emails
  monthly_budget_usd = var.monthly_budget_usd
  oidc_provider_arn  = module.eks.oidc_provider_arn
  oidc_issuer        = module.eks.oidc_issuer
}

# Values the Helm overlay (infra/argocd/values/<env>.yaml) needs, in one place.
output "helm_values" {
  value = {
    global = {
      environment   = var.environment
      domain        = var.zone_name
      dataCidrs     = module.network.data_cidrs
      albCidrs      = module.network.public_cidrs
      endpointCidrs = [var.vpc_cidr]
      image = {
        registry = "${local.account}.dkr.ecr.ap-south-1.amazonaws.com"
      }
    }
    serviceAccounts = { roleArns = module.iam.workload_role_arns }
    config = {
      TALLY_KAFKA_BOOTSTRAP = module.kafka.bootstrap_brokers_sasl_scram
      TALLY_S3_BUCKET       = module.objects.bucket
    }
    ingress = {
      certificateArn = module.dns.regional_certificate_arn
      wafAclArn      = module.waf.arn
    }
  }
}

# REDIS_URL (rediss://:<token>@<endpoint>:6379/0) is assembled into Secrets Manager from these.
output "redis" {
  value = {
    endpoint        = module.cache.primary_endpoint
    auth_secret_arn = module.cache.auth_secret_arn
  }
}

output "database_endpoints" {
  value = { for k, db in module.db : k => db.endpoint }
}

output "database_master_secret_arns" {
  value = { for k, db in module.db : k => db.master_secret_arn }
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "external_secrets_role_arn" {
  value = module.iam.external_secrets_role_arn
}

output "prometheus_remote_write_url" {
  value = module.observability.prometheus_remote_write_url
}

output "ci_release_role_arn" {
  value = module.iam.ci_release_role_arn
}
