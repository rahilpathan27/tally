# Public hosted zone records and DNS-validated ACM certificates for api., console. and vault.
# The regional certificate (ALB) lives in ap-south-1. CloudFront only accepts certificates from
# us-east-1, so a second certificate is issued there through the `aws.us_east_1` alias: it holds
# no customer data and is the one documented exception to the India-only region guardrail.

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = ">= 6.0"
      configuration_aliases = [aws.us_east_1]
    }
  }
}

variable "zone_name" {
  type = string
}

variable "create_zone" {
  type    = bool
  default = false
}

variable "hosts" {
  type    = list(string)
  default = ["api", "console", "vault"]
}

variable "cloudfront_certificate" {
  type    = bool
  default = true
}

resource "aws_route53_zone" "this" {
  count = var.create_zone ? 1 : 0
  name  = var.zone_name
}

data "aws_route53_zone" "this" {
  count = var.create_zone ? 0 : 1
  name  = var.zone_name
}

locals {
  zone_id = var.create_zone ? aws_route53_zone.this[0].zone_id : data.aws_route53_zone.this[0].zone_id
  names   = [for h in var.hosts : "${h}.${var.zone_name}"]
}

resource "aws_acm_certificate" "regional" {
  domain_name               = local.names[0]
  subject_alternative_names = slice(local.names, 1, length(local.names))
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate" "cloudfront" {
  count                     = var.cloudfront_certificate ? 1 : 0
  provider                  = aws.us_east_1
  domain_name               = local.names[0]
  subject_alternative_names = slice(local.names, 1, length(local.names))
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

# Both certificates share the same validation CNAMEs, so one record set validates both.
resource "aws_route53_record" "validation" {
  for_each = {
    for o in aws_acm_certificate.regional.domain_validation_options : o.domain_name => o
  }
  zone_id         = local.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "regional" {
  certificate_arn         = aws_acm_certificate.regional.arn
  validation_record_fqdns = [for r in aws_route53_record.validation : r.fqdn]
}

resource "aws_acm_certificate_validation" "cloudfront" {
  count                   = var.cloudfront_certificate ? 1 : 0
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.cloudfront[0].arn
  validation_record_fqdns = [for r in aws_route53_record.validation : r.fqdn]
}

resource "aws_route53_record" "caa" {
  zone_id = local.zone_id
  name    = var.zone_name
  type    = "CAA"
  ttl     = 3600
  records = ["0 issue \"amazon.com\"", "0 iodef \"mailto:security@${var.zone_name}\""]
}

output "zone_id" {
  value = local.zone_id
}

output "regional_certificate_arn" {
  value = aws_acm_certificate_validation.regional.certificate_arn
}

output "cloudfront_certificate_arn" {
  value = var.cloudfront_certificate ? aws_acm_certificate_validation.cloudfront[0].certificate_arn : null
}
