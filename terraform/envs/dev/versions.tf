terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }

  # Remote state in S3 with native lockfile locking (Terraform 1.10+).
  # Settings come from backend.hcl, created from backend.hcl.example after
  # running terraform/bootstrap:
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}
}

# Credentials come from the standard AWS provider chain (AWS_PROFILE,
# environment variables, SSO, or an instance role). Nothing is hardcoded.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(
      {
        Project     = "RevenueAnalystAgent"
        Environment = var.environment
        ManagedBy   = "Terraform"
      },
      var.additional_tags
    )
  }
}

# CloudFront-scoped WAF WebACLs must be created in us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = merge(
      {
        Project     = "RevenueAnalystAgent"
        Environment = var.environment
        ManagedBy   = "Terraform"
      },
      var.additional_tags
    )
  }
}
