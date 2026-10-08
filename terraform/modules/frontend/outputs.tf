output "cloudfront_domain_name" {
  description = "The distribution's *.cloudfront.net domain name. With a custom domain, point a DNS CNAME or alias record at this name."
  value       = aws_cloudfront_distribution.spa.domain_name
}

output "spa_url" {
  description = "The URL analysts open: the custom domain if set, otherwise the CloudFront domain."
  value       = "https://${var.custom_domain != "" ? var.custom_domain : aws_cloudfront_distribution.spa.domain_name}"
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.spa.id
}

output "spa_bucket_name" {
  value = aws_s3_bucket.spa.id
}
