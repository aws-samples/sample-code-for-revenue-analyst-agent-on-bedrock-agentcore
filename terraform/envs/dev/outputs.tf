output "artifacts_bucket_name" {
  value = module.foundation.artifacts_bucket_name
}

output "athena_workgroup_name" {
  value = module.foundation.athena_workgroup_name
}

output "budget_name" {
  value = module.foundation.budget_name
}
output "tools_lambda_function_name" {
  value = module.tools.lambda_function_name
}

output "tools_lambda_function_arn" {
  value = module.tools.lambda_function_arn
}
output "agent_runtime_arn" {
  value = module.agent.agent_runtime_arn
}

output "gateway_url" {
  value = module.agent.gateway_url
}

output "spa_url" {
  description = "Open this URL in a browser to use the analyst SPA."
  value       = module.frontend.spa_url
}

output "cloudfront_domain_name" {
  description = "The distribution's *.cloudfront.net name. With custom_domain set, create a DNS CNAME or alias record from your domain to this name."
  value       = module.frontend.cloudfront_domain_name
}

output "user_pool_id" {
  description = "Pass to scripts/create_demo_user.sh to create analyst accounts."
  value       = module.identity.user_pool_id
}

output "events_bucket_name" {
  value = module.analytics.bucket_name
}

output "glue_database_name" {
  value = module.analytics.glue_database_name
}
