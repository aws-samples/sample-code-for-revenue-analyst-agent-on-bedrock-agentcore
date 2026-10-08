variable "name_prefix" {
  description = "Prefix applied to all resource names created by this module."
  type        = string
}

variable "user_pool_id" {
  description = "Cognito user pool ID, written into the SPA's runtime config for sign-in."
  type        = string
}

variable "spa_client_id" {
  description = "Cognito app client ID for the SPA."
  type        = string
}

variable "agent_runtime_arn" {
  description = "ARN of the agent runtime the SPA invokes."
  type        = string
}

variable "access_log_retention_days" {
  description = "Days to keep CloudFront access logs before they expire."
  type        = number
  default     = 365

  validation {
    condition     = var.access_log_retention_days >= 1
    error_message = "access_log_retention_days must be at least 1."
  }
}

variable "custom_domain" {
  description = "Optional custom domain for the SPA (for example analyst.example.com). Set together with acm_certificate_arn to serve TLS 1.2+ (TLSv1.2_2021). Leave empty to use the default *.cloudfront.net domain, which CloudFront limits to a TLSv1 minimum."
  type        = string
  default     = ""

  validation {
    condition     = var.custom_domain == "" || can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,}$", var.custom_domain))
    error_message = "custom_domain must be a lowercase DNS name such as analyst.example.com, or empty."
  }
}

variable "acm_certificate_arn" {
  description = "ARN of an ACM certificate in us-east-1 that covers custom_domain. Required when custom_domain is set."
  type        = string
  default     = ""

  validation {
    condition     = (var.acm_certificate_arn == "") == (var.custom_domain == "")
    error_message = "Set custom_domain and acm_certificate_arn together, or leave both empty."
  }

  validation {
    condition     = var.acm_certificate_arn == "" || can(regex("^arn:aws[a-zA-Z-]*:acm:us-east-1:[0-9]{12}:certificate/", var.acm_certificate_arn))
    error_message = "acm_certificate_arn must be an ACM certificate in us-east-1 (CloudFront only uses certificates from that Region)."
  }
}
