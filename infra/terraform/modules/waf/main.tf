# WAFv2 web ACL for the public ALB (REGIONAL) or CloudFront (CLOUDFRONT, created in us-east-1).
# AWS managed rule groups plus per-IP rate limits; the merchant API has its own tighter budget
# because application-level rate limiting (Redis) is the primary control and WAF is the backstop.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0" }
  }
}

variable "name" {
  type = string
}

variable "scope" {
  type    = string
  default = "REGIONAL"
}

variable "rate_limit_per_5m" {
  type    = number
  default = 3000
}

variable "api_rate_limit_per_5m" {
  type    = number
  default = 1500
}

variable "logs_kms_key_arn" {
  type    = string
  default = null
}

locals {
  managed = {
    AWSManagedRulesCommonRuleSet          = 10
    AWSManagedRulesSQLiRuleSet            = 30
    AWSManagedRulesAmazonIpReputationList = 40
    AWSManagedRulesAnonymousIpList        = 50
  }
}

resource "aws_wafv2_web_acl" "this" {
  name  = var.name
  scope = var.scope

  default_action {
    allow {}
  }

  dynamic "rule" {
    for_each = local.managed
    content {
      name     = rule.key
      priority = rule.value
      override_action {
        none {}
      }
      statement {
        managed_rule_group_statement {
          name        = rule.key
          vendor_name = "AWS"
        }
      }
      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = rule.key
        sampled_requests_enabled   = true
      }
    }
  }

  # Known bad inputs, including Log4j (CVE-2021-44228) lookups.
  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 20
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "AWSManagedRulesKnownBadInputsRuleSet"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "api-rate-limit"
    priority = 60
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.api_rate_limit_per_5m
        aggregate_key_type = "IP"
        scope_down_statement {
          byte_match_statement {
            search_string         = "/v1/"
            positional_constraint = "STARTS_WITH"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "api-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "global-rate-limit"
    priority = 70
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.rate_limit_per_5m
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "global-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = var.name
    sampled_requests_enabled   = true
  }
}

# WAF log group names must start with aws-waf-logs-.
resource "aws_cloudwatch_log_group" "this" {
  name              = "aws-waf-logs-${var.name}"
  retention_in_days = 365
  kms_key_id        = var.logs_kms_key_arn
}

resource "aws_wafv2_web_acl_logging_configuration" "this" {
  resource_arn            = aws_wafv2_web_acl.this.arn
  log_destination_configs = [aws_cloudwatch_log_group.this.arn]
  # Never log credentials or signatures.
  redacted_fields {
    single_header {
      name = "authorization"
    }
  }
  redacted_fields {
    single_header {
      name = "x-tally-signature"
    }
  }
  redacted_fields {
    single_header {
      name = "cookie"
    }
  }
}

output "arn" {
  value = aws_wafv2_web_acl.this.arn
}
