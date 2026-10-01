# Least-privilege IAM for workloads (IRSA) and CI (GitHub OIDC). No long-lived access keys
# exist anywhere: pods exchange service-account tokens, CI exchanges its OIDC token.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0" }
  }
}

variable "name" {
  description = "Prefix, e.g. tally-prod."
  type        = string
}

variable "namespace" {
  type = string
}

variable "oidc_provider_arn" {
  type = string
}

variable "oidc_issuer" {
  type = string
}

variable "objects_bucket_arn" {
  type = string
}

variable "objects_kms_key_arn" {
  type = string
}

variable "secrets_kms_key_arn" {
  type = string
}

variable "secrets_prefix" {
  description = "Secrets Manager path the External Secrets Operator may read, e.g. tally/prod."
  type        = string
}

variable "github_repository" {
  description = "owner/repo allowed to assume the CI roles."
  type        = string
}

variable "create_github_oidc_provider" {
  type    = bool
  default = true
}

variable "ecr_repository_arns" {
  description = "Repositories CI may push to (empty in accounts that only pull)."
  type        = list(string)
  default     = []
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  issuer = replace(var.oidc_issuer, "https://", "")
  bucket = var.objects_bucket_arn
  # role => { service accounts, S3 prefixes read, S3 prefixes written }
  workloads = {
    core       = { sas = ["tally-core", "tally-core-worker"], read = ["payouts/", "evidence/"], write = ["payouts/", "evidence/"] }
    recon      = { sas = ["tally-recon"], read = ["recon/", "payouts/"], write = ["recon/"] }
    backoffice = { sas = ["tally-backoffice"], read = ["evidence/", "recon/"], write = [] }
    vault      = { sas = ["tally-vault"], read = [], write = [] }
    jobs       = { sas = ["tally-jobs"], read = ["audit-anchors/"], write = ["audit-anchors/"] }
  }
}

data "aws_iam_policy_document" "irsa_assume" {
  for_each = local.workloads
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:sub"
      values   = [for sa in each.value.sas : "system:serviceaccount:${var.namespace}:${sa}"]
    }
  }
}

resource "aws_iam_role" "workload" {
  for_each             = local.workloads
  name                 = "${var.name}-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.irsa_assume[each.key].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "workload" {
  for_each = { for k, v in local.workloads : k => v if length(v.read) + length(v.write) > 0 }

  dynamic "statement" {
    for_each = length(each.value.read) > 0 ? [1] : []
    content {
      sid       = "Read"
      actions   = ["s3:GetObject", "s3:GetObjectVersion"]
      resources = [for p in each.value.read : "${local.bucket}/${p}*"]
    }
  }
  dynamic "statement" {
    for_each = length(each.value.write) > 0 ? [1] : []
    content {
      sid       = "Write"
      actions   = ["s3:PutObject"]
      resources = [for p in each.value.write : "${local.bucket}/${p}*"]
    }
  }
  statement {
    sid       = "ObjectKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.objects_kms_key_arn]
  }
}

resource "aws_iam_role_policy" "workload" {
  for_each = data.aws_iam_policy_document.workload
  name     = "objects"
  role     = aws_iam_role.workload[each.key].id
  policy   = each.value.json
}

# External Secrets Operator: read-only access to this environment's secrets.
data "aws_iam_policy_document" "eso_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:sub"
      values   = ["system:serviceaccount:external-secrets:external-secrets"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "external_secrets" {
  name               = "${var.name}-external-secrets"
  assume_role_policy = data.aws_iam_policy_document.eso_assume.json
}

data "aws_iam_policy_document" "external_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:aws:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:${var.secrets_prefix}/*"]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [var.secrets_kms_key_arn]
  }
}

resource "aws_iam_role_policy" "external_secrets" {
  name   = "read-env-secrets"
  role   = aws_iam_role.external_secrets.id
  policy = data.aws_iam_policy_document.external_secrets.json
}

# GitHub Actions: `release` (main only) pushes images; `plan` (any ref) is read-only.
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  github_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_assume" {
  for_each = {
    release = ["repo:${var.github_repository}:ref:refs/heads/main"]
    plan    = ["repo:${var.github_repository}:*"]
  }
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = each.value
    }
  }
}

resource "aws_iam_role" "ci_release" {
  count              = length(var.ecr_repository_arns) > 0 ? 1 : 0
  name               = "${var.name}-ci-release"
  assume_role_policy = data.aws_iam_policy_document.github_assume["release"].json
}

data "aws_iam_policy_document" "ci_release" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = var.ecr_repository_arns
  }
}

resource "aws_iam_role_policy" "ci_release" {
  count  = length(var.ecr_repository_arns) > 0 ? 1 : 0
  name   = "push-images"
  role   = aws_iam_role.ci_release[0].id
  policy = data.aws_iam_policy_document.ci_release.json
}

resource "aws_iam_role" "ci_plan" {
  name               = "${var.name}-ci-plan"
  assume_role_policy = data.aws_iam_policy_document.github_assume["plan"].json
}

resource "aws_iam_role_policy_attachment" "ci_plan" {
  role       = aws_iam_role.ci_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

output "workload_role_arns" {
  description = "Feed into the Helm values serviceAccounts.roleArns (core-worker shares core)."
  value = merge(
    { for k, r in aws_iam_role.workload : k => r.arn },
    { "core-worker" = aws_iam_role.workload["core"].arn },
  )
}

output "external_secrets_role_arn" {
  value = aws_iam_role.external_secrets.arn
}

output "ci_release_role_arn" {
  value = length(aws_iam_role.ci_release) > 0 ? aws_iam_role.ci_release[0].arn : null
}

output "ci_plan_role_arn" {
  value = aws_iam_role.ci_plan.arn
}
