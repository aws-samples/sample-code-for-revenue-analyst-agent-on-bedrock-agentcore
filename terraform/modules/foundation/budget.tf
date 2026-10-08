# Cost-monitoring safety net, not required for the agent to function.
#
# A monthly budget with email alerts at 80% and 100%. The filter is scoped to
# the Project=RevenueAnalystAgent tag, which only works after that tag is
# activated as a cost allocation tag in the Billing console (by the
# management account in an AWS Organization). Until then the budget sees $0.
# AWS Budgets alerting itself is free.

resource "aws_budgets_budget" "revenue_analyst_agent" {
  name         = "${var.name_prefix}-budget"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name = "TagKeyValue"
    values = [
      "user:Project$RevenueAnalystAgent"
    ]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
