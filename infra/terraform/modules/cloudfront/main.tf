# CloudFront for the console's hashed static assets only (static.<zone> -> /_next/static/*).
# Pages, the merchant API and the vault are served straight from the ap-south-1 ALB, so no
# payment data, session cookie or card token ever passes through an edge location outside India.
# The origin is the console host itself; CloudFront forwards no cookies or auth headers.

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = ">= 6.0"
      configuration_aliases = [aws.us_east_1]
    }
  }
}

variable "name" {
  type = string
}

variable "zone_id" {
  type = string
}

variable "aliases" {
  description = "e.g. [\"static.tally.example\"]"
  type        = list(string)
}

variable "origin_domain" {
  description = "Console hostname served by the regional ALB (e.g. console.tally.example)."
  type        = string
}

variable "certificate_arn" {
  description = "ACM certificate in us-east-1 covering the aliases."
  type        = string
}

variable "web_acl_arn" {
  description = "WAFv2 ACL with CLOUDFRONT scope (us-east-1); null disables."
  type        = string
  default     = null
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

resource "aws_cloudfront_response_headers_policy" "this" {
  name = "${var.name}-static"
  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 63072000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }
  }
}

resource "aws_cloudfront_distribution" "this" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${var.name} console static assets"
  aliases         = var.aliases
  price_class     = "PriceClass_200" # includes India edge locations
  web_acl_id      = var.web_acl_arn
  http_version    = "http2and3"

  origin {
    origin_id   = "console"
    domain_name = var.origin_domain
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # Only immutable, content-hashed build output is cacheable; everything else is refused.
  ordered_cache_behavior {
    path_pattern               = "/_next/static/*"
    target_origin_id           = "console"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.this.id
  }

  default_cache_behavior {
    target_origin_id       = "console"
    viewer_protocol_policy = "https-only"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id
    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.deny.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = var.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_cloudfront_function" "deny" {
  name    = "${var.name}-deny-non-static"
  runtime = "cloudfront-js-2.0"
  comment = "Only /_next/static/* is served from the edge"
  publish = true
  code    = <<-JS
    function handler(event) {
      return { statusCode: 404, statusDescription: "Not Found" };
    }
  JS
}

resource "aws_route53_record" "alias" {
  for_each = toset(var.aliases)
  zone_id  = var.zone_id
  name     = each.key
  type     = "A"
  alias {
    name                   = aws_cloudfront_distribution.this.domain_name
    zone_id                = aws_cloudfront_distribution.this.hosted_zone_id
    evaluate_target_health = false
  }
}

output "domain_name" {
  value = aws_cloudfront_distribution.this.domain_name
}
