# Two repositories (tally/python, tally/web). Tags are immutable, so a digest promoted to prod
# can never be swapped underneath a tag; images are scanned on push and encrypted with KMS.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0" }
  }
}

variable "repositories" {
  type    = set(string)
  default = ["tally/python", "tally/web"]
}

variable "kms_key_arn" {
  type = string
}

variable "pull_account_ids" {
  description = "Other accounts (staging, prod) allowed to pull images built once in this one."
  type        = list(string)
  default     = []
}

resource "aws_ecr_repository" "this" {
  for_each             = var.repositories
  name                 = each.key
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = var.repositories
  repository = aws_ecr_repository.this[each.key].name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 14 days"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 14 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the newest 200 images"
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 200 }
        action       = { type = "expire" }
      },
    ]
  })
}

data "aws_iam_policy_document" "pull" {
  count = length(var.pull_account_ids) > 0 ? 1 : 0
  statement {
    sid     = "CrossAccountPull"
    actions = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    principals {
      type        = "AWS"
      identifiers = [for id in var.pull_account_ids : "arn:aws:iam::${id}:root"]
    }
  }
}

resource "aws_ecr_repository_policy" "pull" {
  for_each   = length(var.pull_account_ids) > 0 ? var.repositories : toset([])
  repository = aws_ecr_repository.this[each.key].name
  policy     = data.aws_iam_policy_document.pull[0].json
}

output "repository_arns" {
  value = { for name, repo in aws_ecr_repository.this : name => repo.arn }
}

output "repository_urls" {
  value = { for name, repo in aws_ecr_repository.this : name => repo.repository_url }
}
