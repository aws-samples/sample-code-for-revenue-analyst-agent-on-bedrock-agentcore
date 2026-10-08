output "artifacts_bucket_name" {
  description = "Name of the shared artifacts bucket (reports/ and athena-results/ prefixes)."
  value       = aws_s3_bucket.artifacts.bucket
}

output "artifacts_bucket_arn" {
  description = "ARN of the shared artifacts bucket."
  value       = aws_s3_bucket.artifacts.arn
}

output "athena_workgroup_name" {
  description = "Name of the Athena workgroup used by query_analytics."
  value       = aws_athena_workgroup.main.name
}

output "athena_workgroup_arn" {
  description = "ARN of the Athena workgroup, for IAM scoping in other modules."
  value       = aws_athena_workgroup.main.arn
}

output "budget_name" {
  description = "Name of the tag-scoped cost budget."
  value       = aws_budgets_budget.revenue_analyst_agent.name
}
