# SPA hosting bucket -- private, no public access whatsoever. CloudFront
# reaches it exclusively via Origin Access Control (OAC), never a public
# website endpoint or bucket policy allowing "*". This is the only surface
# in this build that's internet-facing at all, so it gets the strictest
# treatment: private bucket + OAC + WAF + CloudFront-only TLS.

resource "aws_s3_bucket" "spa" {
  force_destroy = true # build output only; re-uploaded on apply
  bucket        = "${var.name_prefix}-spa-${data.aws_caller_identity.current.account_id}"

  tags = {
    Name = "${var.name_prefix}-spa"
  }
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_public_access_block" "spa" {
  bucket                  = aws_s3_bucket.spa.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "spa" {
  bucket = aws_s3_bucket.spa.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "spa" {
  bucket = aws_s3_bucket.spa.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Bucket policy grants read access to ONLY this CloudFront distribution's
# OAC (via the aws:SourceArn condition) -- not to CloudFront generally, and
# never directly to any principal reachable from the public internet.
resource "aws_s3_bucket_policy" "spa" {
  bucket = aws_s3_bucket.spa.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontOACOnly"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.spa.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.spa.arn
          }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.spa.arn,
          "${aws_s3_bucket.spa.arn}/*"
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
