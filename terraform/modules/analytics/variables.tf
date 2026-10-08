variable "name_prefix" {
  description = "Prefix applied to all resource names created by this module."
  type        = string
}

variable "sample_days" {
  description = "Days of sample event history to generate, ending on the day you run terraform apply. 45 days is about 18 MB."
  type        = number
  default     = 45

  validation {
    condition     = var.sample_days >= 1 && var.sample_days <= 366
    error_message = "sample_days must be between 1 and 366."
  }
}
