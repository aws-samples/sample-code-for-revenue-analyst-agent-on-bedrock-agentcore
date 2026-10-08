# Sample event lake for the query_analytics tool: an S3 bucket of gzipped
# JSON events, a Glue database and table over it, and a step that generates
# and uploads the data (scripts/generate_sample_events.py).
#
# The table uses partition projection, so no crawler or MSCK REPAIR is
# needed: Athena computes partition locations from the query's predicates.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_s3_bucket" "events" {
  bucket        = "${var.name_prefix}-events-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # sample data only; regenerated on apply

  tags = {
    Name = "${var.name_prefix}-events"
  }
}

resource "aws_s3_bucket_public_access_block" "events" {
  bucket                  = aws_s3_bucket.events.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "events" {
  bucket = aws_s3_bucket.events.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "events" {
  bucket = aws_s3_bucket.events.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "events" {
  bucket = aws_s3_bucket.events.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_policy" "events_tls_only" {
  bucket = aws_s3_bucket.events.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.events.arn, "${aws_s3_bucket.events.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.events]
}

resource "aws_glue_catalog_database" "analytics" {
  name        = replace("${var.name_prefix}_analytics", "-", "_")
  description = "Sample hotel event lake for the Revenue Analyst Agent."
}

resource "aws_glue_catalog_table" "events" {
  name          = "events"
  database_name = aws_glue_catalog_database.analytics.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "classification"                     = "json"
    "compressionType"                    = "gzip"
    "projection.enabled"                 = "true"
    "projection.source_partition.type"   = "enum"
    "projection.source_partition.values" = "crs,pms,billing,loyalty,housekeeping,audit,payment"
    "projection.year.type"               = "integer"
    "projection.year.range"              = "2024,2030"
    "projection.month.type"              = "integer"
    "projection.month.range"             = "1,12"
    "projection.day.type"                = "integer"
    "projection.day.range"               = "1,31"
    "storage.location.template"          = "s3://${aws_s3_bucket.events.id}/events/source_partition=$${source_partition}/year=$${year}/month=$${month}/day=$${day}/"
  }

  partition_keys {
    name = "source_partition"
    type = "string"
  }
  partition_keys {
    name = "year"
    type = "int"
  }
  partition_keys {
    name = "month"
    type = "int"
  }
  partition_keys {
    name = "day"
    type = "int"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.events.id}/events/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
    }

    columns {
      name = "event_id"
      type = "string"
    }
    columns {
      name = "source"
      type = "string"
    }
    columns {
      name = "detail_type"
      type = "string"
    }
    columns {
      name = "event_time"
      type = "string"
    }
    columns {
      name = "region"
      type = "string"
    }
    columns {
      name = "account"
      type = "string"
    }
    columns {
      name = "detail"
      type = "string"
    }
  }
}

# Generate the events locally and sync them to the bucket. Re-runs when the
# generator, the sample data model, or the day count changes. To refresh the
# data so it ends today, run:
#   terraform apply -replace=module.analytics.null_resource.load_sample_events
resource "null_resource" "load_sample_events" {
  triggers = {
    generator   = filesha256("${path.module}/../../../scripts/generate_sample_events.py")
    sample_data = filesha256("${path.module}/../../../tools/lib/sample_data.py")
    days        = tostring(var.sample_days)
    bucket      = aws_s3_bucket.events.id
  }

  provisioner "local-exec" {
    command     = <<-EOT
      set -euo pipefail
      python3 "${path.module}/../../../scripts/generate_sample_events.py" --out "${path.module}/build/events" --days ${var.sample_days}
      aws s3 sync "${path.module}/build/events" "s3://${aws_s3_bucket.events.id}/events/" --delete --only-show-errors --region ${data.aws_region.current.region}
    EOT
    interpreter = ["bash", "-c"]
  }

  depends_on = [aws_s3_bucket_policy.events_tls_only]
}
