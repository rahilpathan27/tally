# Managed telemetry backends. In-cluster Prometheus (kube-prometheus-stack) evaluates the
# alert rules shipped with the Helm chart and remote-writes to Amazon Managed Prometheus for
# durable storage; Alertmanager pages through SNS. Application logs go to CloudWatch (KMS).
# A monthly budget alarm guards against runaway cost.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0" }
  }
}

variable "name" {
  type = string
}

variable "logs_kms_key_arn" {
  type = string
}

variable "log_retention_days" {
  type    = number
  default = 365
}

variable "alert_emails" {
  type    = list(string)
  default = []
}

variable "monthly_budget_usd" {
  type = number
}

variable "oidc_provider_arn" {
  type = string
}

variable "oidc_issuer" {
  type = string
}

resource "aws_prometheus_workspace" "this" {
  alias       = var.name
  kms_key_arn = var.logs_kms_key_arn
  logging_configuration {
    log_group_arn = "${aws_cloudwatch_log_group.amp.arn}:*"
  }
}

resource "aws_cloudwatch_log_group" "amp" {
  name              = "/tally/${var.name}/amp"
  retention_in_days = 365
  kms_key_id        = var.logs_kms_key_arn
}

resource "aws_cloudwatch_log_group" "apps" {
  name              = "/tally/${var.name}/applications"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn
}

resource "aws_sns_topic" "alerts" {
  name              = "${var.name}-alerts"
  kms_master_key_id = var.logs_kms_key_arn
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.alert_emails)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.key
}

# Prometheus (remote write) and Alertmanager (SNS publish) run with IRSA.
locals {
  issuer = replace(var.oidc_issuer, "https://", "")
}

data "aws_iam_policy_document" "prometheus_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringLike"
      variable = "${local.issuer}:sub"
      values = [
        "system:serviceaccount:monitoring:kube-prometheus-stack-prometheus",
        "system:serviceaccount:monitoring:kube-prometheus-stack-alertmanager",
      ]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "prometheus" {
  name               = "${var.name}-prometheus"
  assume_role_policy = data.aws_iam_policy_document.prometheus_assume.json
}

resource "aws_iam_role_policy" "prometheus" {
  role = aws_iam_role.prometheus.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["aps:RemoteWrite"], Resource = aws_prometheus_workspace.this.arn },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.alerts.arn },
      { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = var.logs_kms_key_arn },
    ]
  })
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [80, 100]
    content {
      comparison_operator       = "GREATER_THAN"
      threshold                 = notification.value
      threshold_type            = "PERCENTAGE"
      notification_type         = notification.value == 100 ? "FORECASTED" : "ACTUAL"
      subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
    }
  }
}

output "prometheus_remote_write_url" {
  value = "${aws_prometheus_workspace.this.prometheus_endpoint}api/v1/remote_write"
}

output "prometheus_role_arn" {
  value = aws_iam_role.prometheus.arn
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
