variable "aws_region" {
  description = "Region to deploy into. It must offer Bedrock AgentCore and the Claude models set in the agent module (the defaults use US cross-Region inference profiles)."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name. Change it to deploy a second copy in the same account."
  type        = string
  default     = "revagent-dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.name_prefix))
    error_message = "name_prefix must be 3-21 lowercase letters, digits, or hyphens, starting with a letter."
  }
}

variable "environment" {
  description = "Value for the Environment tag."
  type        = string
  default     = "dev"
}

variable "additional_tags" {
  description = "Extra tags applied to every resource, for example { Owner = \"team-name\" }."
  type        = map(string)
  default     = {}
}

variable "notification_email" {
  description = "Email address for budget alerts and as the SES sender for report-ready emails. SES sends a verification link to it on first apply."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.notification_email))
    error_message = "notification_email must be a valid email address."
  }
}

variable "budget_limit_usd" {
  description = "Monthly budget (USD) for the tag-scoped budget alert."
  type        = number
  default     = 50
}

variable "sample_days" {
  description = "Days of sample event history to load into the Athena event lake."
  type        = number
  default     = 45
}

variable "cognito_mfa_configuration" {
  description = "Cognito MFA setting for analyst sign-in: OFF, OPTIONAL, or ON."
  type        = string
  default     = "OPTIONAL"
}

variable "custom_domain" {
  description = "Optional custom domain for the analyst SPA (for example analyst.example.com). With acm_certificate_arn, CloudFront enforces TLS 1.2+ (TLSv1.2_2021). Leave empty to use the *.cloudfront.net domain (TLSv1 minimum)."
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "ACM certificate ARN in us-east-1 covering custom_domain. Required when custom_domain is set."
  type        = string
  default     = ""
}
