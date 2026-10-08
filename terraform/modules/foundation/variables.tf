variable "name_prefix" {
  description = "Prefix applied to all resource names created by this module."
  type        = string
}

variable "artifacts_lifecycle_expiration_days" {
  description = "Number of days after which generated artifacts (reports/exports) are expired. Kept short for data minimization."
  type        = number
  default     = 30
}

variable "athena_scan_limit_bytes" {
  description = "Per-query data scanned limit for the Athena workgroup, to bound cost from any single query (including agent-issued ones)."
  type        = number
  default     = 1073741824 # 1 GB
}

variable "budget_limit_usd" {
  description = "Monthly budget limit (USD) for the tag-scoped AWS Budget alert on Project=RevenueAnalystAgent."
  type        = number
  default     = 50
}

variable "budget_alert_email" {
  description = "Email address to notify when the RevenueAnalystAgent budget threshold is exceeded."
  type        = string
}

variable "report_retention_days" {
  description = "Days to keep generated PDF reports under reports/ in the artifacts bucket before they expire."
  type        = number
  default     = 30

  validation {
    condition     = var.report_retention_days >= 1
    error_message = "report_retention_days must be at least 1."
  }
}
