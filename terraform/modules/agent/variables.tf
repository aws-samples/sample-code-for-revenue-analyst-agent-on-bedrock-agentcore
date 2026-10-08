variable "name_prefix" {
  description = "Prefix applied to all resource names created by this module."
  type        = string
}

# Multi-model strategy: Haiku routes, Sonnet reasons (baseline), Opus
# handles heavy multi-step analysis. Pinned, versioned
# inference profile IDs -- not un-dated aliases that could shift silently.
variable "agent_haiku_model_id" {
  description = "Pinned Bedrock inference profile ID for the cheap/fast complexity-routing model (Haiku)."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "agent_sonnet_model_id" {
  description = "Pinned Bedrock inference profile ID for the baseline reasoning model (Sonnet)."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-6"
}

variable "agent_opus_model_id" {
  description = "Pinned Bedrock inference profile ID for the heavy multi-step analysis model (Opus)."
  type        = string
  default     = "us.anthropic.claude-opus-4-8"
}

variable "tools_lambda_arn" {
  description = "ARN of the tools Lambda that backs the Gateway's Lambda target."
  type        = string
}

variable "tools_lambda_name" {
  description = "Name of the tools Lambda, used to scope the Gateway's invoke permission."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the agent runtime's log group. Defaults to 365 days (1 year). Lower it to reduce log storage cost in a short-lived demo."
  type        = number
  default     = 365
}

# --- generate_report (local agent tool, writes directly to S3) ---

variable "artifacts_bucket_arn" {
  description = "ARN of the artifacts bucket, so the Runtime's own role can write reports/ objects directly (see runtime.tf's GenerateReportWriteAccess)."
  type        = string
}

variable "artifacts_bucket_name" {
  description = "Name of the artifacts bucket, passed to the agent as an env var (boto3 put_object takes a bucket name, not ARN)."
  type        = string
}

# --- Inbound CUSTOM_JWT authorizer ---

variable "user_pool_id" {
  description = "Cognito user pool whose ID tokens the Runtime's CUSTOM_JWT authorizer accepts."
  type        = string
}

variable "spa_client_id" {
  description = "Cognito app client ID. ID tokens carry it as the `aud` claim, which the authorizer checks."
  type        = string
}
