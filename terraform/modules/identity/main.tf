# Cognito user pool for analysts. The SPA signs users in with SRP, and the
# AgentCore Runtime's CUSTOM_JWT authorizer validates the resulting ID
# tokens (see ../agent/runtime.tf).
#
# Access model, enforced in tools/lib/reporting_client.py:
#   RevenueManager group  -> every property
#   RegionalManager group -> only properties in the user's custom:region
#
# Users are created by scripts/create_demo_user.sh, not by Terraform, so no
# password ever lands in Terraform state.

resource "aws_cognito_user_pool" "analysts" {
  name = "${var.name_prefix}-analysts"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Accounts are created by an administrator; there is no self sign-up.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  mfa_configuration = var.mfa_configuration
  dynamic "software_token_mfa_configuration" {
    for_each = var.mfa_configuration == "OFF" ? [] : [1]
    content {
      enabled = true
    }
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  schema {
    name                     = "region"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false
    string_attribute_constraints {
      min_length = 1
      max_length = 32
    }
  }

  deletion_protection = var.deletion_protection ? "ACTIVE" : "INACTIVE"

  tags = {
    Name = "${var.name_prefix}-analysts"
  }
}

resource "aws_cognito_user_group" "revenue_manager" {
  user_pool_id = aws_cognito_user_pool.analysts.id
  name         = "RevenueManager"
  description  = "Chain-level access to every property."
}

resource "aws_cognito_user_group" "regional_manager" {
  user_pool_id = aws_cognito_user_pool.analysts.id
  name         = "RegionalManager"
  description  = "Access to properties in the user's custom:region only."
}

# Browser client: no secret, SRP only. custom:region is readable but NOT
# writable through this client, so a signed-in user cannot change their own
# region and widen their access. Only an administrator can set it.
resource "aws_cognito_user_pool_client" "spa" {
  name         = "${var.name_prefix}-spa"
  user_pool_id = aws_cognito_user_pool.analysts.id

  generate_secret                      = false
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  allowed_oauth_flows_user_pool_client = false

  read_attributes  = ["email", "email_verified", "name", "custom:region"]
  write_attributes = ["name"]

  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 12
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "hours"
  }
}
