variable "name_prefix" {
  description = "Prefix applied to all resource names created by this module."
  type        = string
}

variable "mfa_configuration" {
  description = "Cognito MFA setting: OFF, OPTIONAL, or ON. The sample SPA does not implement the TOTP enrollment or challenge screens, so leave this OPTIONAL for the demo and do not enroll demo users in MFA. Require MFA (ON) and add those screens, or use the Cognito managed login, before any non-demo use."
  type        = string
  default     = "OPTIONAL"

  validation {
    condition     = contains(["OFF", "OPTIONAL", "ON"], var.mfa_configuration)
    error_message = "mfa_configuration must be OFF, OPTIONAL, or ON."
  }
}

variable "deletion_protection" {
  description = "Protect the user pool from deletion. Off by default so the sample can be destroyed cleanly."
  type        = bool
  default     = false
}
