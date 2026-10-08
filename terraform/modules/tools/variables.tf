variable "name_prefix" {
  description = "Prefix applied to all resource names created by this module."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the tools Lambda. Defaults to 365 days (1 year). Lower it to reduce log storage cost in a short-lived demo."
  type        = number
  default     = 365
}

# --- query_analytics (Athena over the sample event lake) ---

variable "analytics_glue_database" {
  description = "Glue database holding the events table (from the analytics module)."
  type        = string
}

variable "analytics_glue_table" {
  description = "Glue table name for the sample events."
  type        = string
}

variable "analytics_bucket_arn" {
  description = "ARN of the S3 bucket holding the sample event files (read-only access)."
  type        = string
}

variable "analytics_workgroup_name" {
  description = "Athena workgroup (from the foundation module) that query_analytics runs in."
  type        = string
}

variable "analytics_workgroup_arn" {
  description = "ARN of the Athena workgroup, for IAM scoping."
  type        = string
}

variable "artifacts_bucket_arn" {
  description = "ARN of the artifacts bucket (athena-results/, reports/, recommendations/ prefixes)."
  type        = string
}

variable "artifacts_bucket_name" {
  description = "Name of the artifacts bucket."
  type        = string
}

# --- Report delivery via SES ---

variable "report_notification_email" {
  description = "Fallback report recipient, used only when a report object has no analyst-email metadata."
  type        = string
}

variable "report_sender_email" {
  description = "SES-verified From address for report-ready emails. SES sends a verification link to this address on first apply."
  type        = string
}

variable "cognito_user_pool_id" {
  description = "Cognito user pool that issues analyst ID tokens. The tools Lambda verifies every caller token against this pool's JWKS."
  type        = string
}

variable "cognito_app_client_id" {
  description = "Cognito app client ID the analyst ID tokens are issued for (the token's aud claim)."
  type        = string
}
