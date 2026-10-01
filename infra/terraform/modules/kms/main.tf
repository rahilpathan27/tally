# One customer-managed key per data class, so a compromise or a grant is scoped to that class
# (ledger database keys cannot decrypt vault data, and so on). Rotation is yearly.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0" }
  }
}

variable "name" {
  type = string
}

variable "purposes" {
  description = "Key purposes, e.g. general-db, ledger-db, vault-db, objects, secrets, logs, kafka, eks."
  type        = set(string)
}

variable "deletion_window_days" {
  type    = number
  default = 30
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  root    = "arn:${data.aws_partition.current.partition}:iam::${local.account}:root"
}

data "aws_iam_policy_document" "key" {
  for_each = var.purposes

  statement {
    sid       = "AccountAdministration"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [local.root]
    }
  }

  dynamic "statement" {
    for_each = each.key == "logs" ? [1] : []
    content {
      sid       = "CloudWatchLogs"
      actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
      resources = ["*"]
      principals {
        type        = "Service"
        identifiers = ["logs.${data.aws_region.current.region}.amazonaws.com"]
      }
      condition {
        test     = "ArnLike"
        variable = "kms:EncryptionContext:aws:logs:arn"
        values   = ["arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${local.account}:*"]
      }
    }
  }

  dynamic "statement" {
    for_each = each.key == "audit" ? [1] : []
    content {
      sid       = "CloudTrail"
      actions   = ["kms:GenerateDataKey*", "kms:DescribeKey"]
      resources = ["*"]
      principals {
        type        = "Service"
        identifiers = ["cloudtrail.amazonaws.com"]
      }
      condition {
        test     = "StringLike"
        variable = "kms:EncryptionContext:aws:cloudtrail:arn"
        values   = ["arn:${data.aws_partition.current.partition}:cloudtrail:*:${local.account}:trail/*"]
      }
    }
  }
}

resource "aws_kms_key" "this" {
  for_each = var.purposes

  description             = "${var.name} ${each.key}"
  enable_key_rotation     = true
  rotation_period_in_days = 365
  deletion_window_in_days = var.deletion_window_days
  policy                  = data.aws_iam_policy_document.key[each.key].json
  tags                    = { Purpose = each.key }
}

resource "aws_kms_alias" "this" {
  for_each      = var.purposes
  name          = "alias/${var.name}-${each.key}"
  target_key_id = aws_kms_key.this[each.key].key_id
}

output "key_arns" {
  value = { for purpose, key in aws_kms_key.this : purpose => key.arn }
}
