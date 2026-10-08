module "foundation" {
  source = "../../modules/foundation"

  name_prefix        = var.name_prefix
  budget_alert_email = var.notification_email
  budget_limit_usd   = var.budget_limit_usd
}

module "identity" {
  source = "../../modules/identity"

  name_prefix       = var.name_prefix
  mfa_configuration = var.cognito_mfa_configuration
}

module "analytics" {
  source = "../../modules/analytics"

  name_prefix = var.name_prefix
  sample_days = var.sample_days
}

module "tools" {
  source = "../../modules/tools"

  name_prefix = var.name_prefix

  analytics_glue_database  = module.analytics.glue_database_name
  analytics_glue_table     = module.analytics.glue_table_name
  analytics_bucket_arn     = module.analytics.bucket_arn
  analytics_workgroup_name = module.foundation.athena_workgroup_name
  analytics_workgroup_arn  = module.foundation.athena_workgroup_arn
  artifacts_bucket_arn     = module.foundation.artifacts_bucket_arn
  artifacts_bucket_name    = module.foundation.artifacts_bucket_name

  # The tools Lambda verifies each analyst token itself (signature, issuer,
  # audience, token_use, expiry) against this user pool.
  cognito_user_pool_id  = module.identity.user_pool_id
  cognito_app_client_id = module.identity.spa_client_id

  # Reports normally go to the requesting analyst's own email (from their
  # token). This address is the verified SES sender and the fallback
  # recipient for a token without an email claim.
  report_sender_email       = var.notification_email
  report_notification_email = var.notification_email
}

module "agent" {
  source = "../../modules/agent"

  name_prefix = var.name_prefix

  tools_lambda_arn  = module.tools.lambda_function_arn
  tools_lambda_name = module.tools.lambda_function_name

  artifacts_bucket_arn  = module.foundation.artifacts_bucket_arn
  artifacts_bucket_name = module.foundation.artifacts_bucket_name

  user_pool_id  = module.identity.user_pool_id
  spa_client_id = module.identity.spa_client_id
}

module "frontend" {
  source = "../../modules/frontend"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name_prefix       = var.name_prefix
  user_pool_id      = module.identity.user_pool_id
  spa_client_id     = module.identity.spa_client_id
  agent_runtime_arn = module.agent.agent_runtime_arn

  # Optional: serve the SPA on your own domain with TLS 1.2+.
  custom_domain       = var.custom_domain
  acm_certificate_arn = var.acm_certificate_arn
}

# The Gateway's invoke permission on the tools Lambda moved from the tools
# module to the agent module, so it can be scoped to the Gateway's ARN. This
# block lets existing deployments rename it in place instead of deleting and
# re-creating it.
moved {
  from = module.tools.aws_lambda_permission.allow_agentcore_gateway
  to   = module.agent.aws_lambda_permission.allow_agentcore_gateway
}
