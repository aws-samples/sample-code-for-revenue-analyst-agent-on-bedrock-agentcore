# Artifacts bucket for everything the agent and tools write:
#   reports/          generated PDF reports (generate_report)
#   recommendations/  PENDING / CONFIRMED recommendation records
#   athena-results/   Athena query results (see athena.tf)
# One bucket with prefix-scoped lifecycle rules keeps these use cases
# separate without extra resources.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.name_prefix}-artifacts-${data.aws_caller_identity.current.account_id}"

  tags = {
    Name = "${var.name_prefix}-artifacts"
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

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

  # Generated PDF reports are delivered by a short-lived pre-signed link
  # (see tools/lib/report_delivery.py), so they do not need to be kept
  # indefinitely. Expire them after var.report_retention_days.
  rule {
    id     = "expire-generated-reports"
    status = "Enabled"
    filter {
      prefix = "reports/"
    }
    expiration {
      days = var.report_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.report_retention_days
    }
  }

  rule {
    id     = "expire-reports"
    status = "Enabled"
    filter {
      prefix = "reports/"
    }
    expiration {
      days = var.artifacts_lifecycle_expiration_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.artifacts_lifecycle_expiration_days
    }
  }

  rule {
    id     = "expire-athena-results"
    status = "Enabled"
    filter {
      prefix = "athena-results/"
    }
    expiration {
      days = 14
    }
    noncurrent_version_expiration {
      noncurrent_days = 14
    }
  }

  # recommendations/ (gated-write audit records) intentionally has
  # NO expiration on the CURRENT version -- this is an immutable audit trail
  # (each record's PENDING -> CONFIRMED transition is itself part of the
  # record's history), not a disposable artifact
  # like reports/ or athena-results/, so it must not silently disappear.
  # Only NONCURRENT versions (the pre-CONFIRMED PENDING write, superseded
  # once record_recommendation runs) are expired, since the current version
  # already carries everything needed and versioning's only role here is
  # proving the transition happened, not retaining every historical byte
  # forever.
  rule {
    id     = "expire-noncurrent-recommendation-versions"
    status = "Enabled"
    filter {
      prefix = "recommendations/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts_tls_only" {
  bucket = aws_s3_bucket.artifacts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.artifacts.arn,
          "${aws_s3_bucket.artifacts.arn}/*"
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
