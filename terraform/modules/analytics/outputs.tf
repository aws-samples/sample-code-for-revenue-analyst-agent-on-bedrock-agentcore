output "bucket_name" {
  value = aws_s3_bucket.events.id
}

output "bucket_arn" {
  value = aws_s3_bucket.events.arn
}

output "glue_database_name" {
  value = aws_glue_catalog_database.analytics.name
}

output "glue_table_name" {
  value = aws_glue_catalog_table.events.name
}
