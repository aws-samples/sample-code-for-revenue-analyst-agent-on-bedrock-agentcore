# Dedicated Athena workgroup for query_analytics. It writes results to the
# artifacts bucket (athena-results/ prefix, see artifacts.tf) and enforces a
# per-query scan cap, because a model-issued query could otherwise be
# unbounded and expensive.

resource "aws_athena_workgroup" "main" {
  name = "${var.name_prefix}-workgroup"
  # Lets `terraform destroy` delete the workgroup after queries have run
  # (Athena refuses to delete a workgroup that still has query history).
  force_destroy = true

  configuration {
    enforce_workgroup_configuration    = true
    bytes_scanned_cutoff_per_query     = var.athena_scan_limit_bytes
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.artifacts.bucket}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }

  tags = {
    Name = "${var.name_prefix}-workgroup"
  }
}
