terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.14" }
    random = { source = "hashicorp/random", version = "~> 3.7" }
    tls    = { source = "hashicorp/tls", version = "~> 4.1" }
  }

  # State lives in the account's own bucket (created by infra/terraform/bootstrap) with native
  # S3 locking. Supply the bucket at init time:
  #   terraform init -backend-config="bucket=tally-tfstate-<account-id>"
  backend "s3" {
    key          = "tally/dev/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}

# Guardrail 1: data-bearing resources may only be created in Mumbai (primary) or Hyderabad (DR).
variable "region" {
  type    = string
  default = "ap-south-1"
  validation {
    condition     = contains(["ap-south-1", "ap-south-2"], var.region)
    error_message = "Tally data must stay in India: region must be ap-south-1 or ap-south-2."
  }
}

# Guardrail 2: refuse to run against any account other than this environment's.
variable "account_id" {
  description = "AWS account that hosts tally-dev."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      Project     = "tally"
      Environment = "dev"
      ManagedBy   = "terraform"
      DataClass   = "synthetic"
    }
  }
}

# Only for CloudFront's certificate and edge WAF, which AWS requires in us-east-1. No customer
# data is stored there (see modules/cloudfront and docs/deployment.md).
provider "aws" {
  alias               = "us_east_1"
  region              = "us-east-1"
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      Project     = "tally"
      Environment = "dev"
      ManagedBy   = "terraform"
      DataClass   = "none"
    }
  }
}
