# One-time bootstrap: creates the S3 bucket that holds Terraform state for
# terraform/envs/dev. This config keeps its own state LOCAL, because a
# backend bucket cannot store the state of the config that creates it.
# Run it once, then put the output bucket name in envs/dev/backend.hcl.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  # No backend block — intentional. State for this bootstrap config stays
  # local (terraform/bootstrap/terraform.tfstate, gitignored).
}

variable "aws_region" {
  description = "Region for the state bucket."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for the state bucket name."
  type        = string
  default     = "revagent-dev"
}

# Credentials come from the standard AWS provider chain (AWS_PROFILE etc.).
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "RevenueAnalystAgent"
      ManagedBy = "Terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "tfstate" {
  bucket = "${var.name_prefix}-tfstate-${data.aws_caller_identity.current.account_id}"

  tags = {
    Name = "${var.name_prefix}-tfstate"
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"
    filter {
      prefix = ""
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "tfstate_tls_only" {
  bucket = aws_s3_bucket.tfstate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.tfstate.arn,
          "${aws_s3_bucket.tfstate.arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })
}

output "tfstate_bucket_name" {
  value = aws_s3_bucket.tfstate.bucket
}
