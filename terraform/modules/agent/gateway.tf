# AgentCore Gateway -- exposes the tools Lambda as
# governed MCP tools that the agent's Strands MCPClient can discover and
# call.
#
# Inbound authorizer = AWS_IAM. The only caller is this sample's own
# Runtime, which signs requests with its execution role (SigV4), so no
# separate OAuth client is needed for the Gateway.

resource "aws_iam_role" "gateway" {
  name = "${var.name_prefix}-gateway-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "bedrock-agentcore.amazonaws.com"
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })

  tags = {
    Name = "${var.name_prefix}-gateway-role"
  }
}

resource "aws_bedrockagentcore_gateway" "main" {
  name        = "${var.name_prefix}-gateway"
  description = "Gateway exposing the tools Lambda as governed MCP tools for the Revenue Analyst Agent"
  role_arn    = aws_iam_role.gateway.arn

  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"

  tags = {
    Name = "${var.name_prefix}-gateway"
  }
}

# Allows the Gateway to invoke the tools Lambda. Scoped to this specific
# Lambda ARN only -- the Gateway's role has no other permissions.
# Lets this Gateway, and only this Gateway, invoke the tools Lambda.
# source_arn pins the permission to this Gateway's ARN, so no other
# AgentCore Gateway (in this or any other account) can invoke the function.
resource "aws_lambda_permission" "allow_agentcore_gateway" {
  statement_id   = "AllowBedrockAgentCoreGatewayInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = var.tools_lambda_name
  principal      = "bedrock-agentcore.amazonaws.com"
  source_account = data.aws_caller_identity.current.account_id
  source_arn     = aws_bedrockagentcore_gateway.main.gateway_arn
}

resource "aws_iam_role_policy" "gateway_invoke_tools_lambda" {
  name = "${var.name_prefix}-gateway-invoke-tools-lambda"
  role = aws_iam_role.gateway.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeToolsLambda"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = var.tools_lambda_arn
      }
    ]
  })
}

# Each aws_bedrockagentcore_gateway_target supports exactly ONE tool_schema
# (one tool). To expose all 3 tools from the single tools Lambda, we create
# 3 separate targets, each pointing at the SAME lambda_arn but with a
# different tool_schema -- the Lambda's internal router
# (tools/handler.py::_ROUTES) dispatches by the AgentCore-delivered tool
# name regardless of which target invoked it.
#
# generate_report is intentionally NOT a Gateway target. It is a local
# tool in agent/agent.py that reads the PDF straight from the Code
# Interpreter sandbox and writes to S3 with the Runtime's own role, so PDF
# bytes never pass through the model's context.

resource "aws_bedrockagentcore_gateway_target" "get_range_metrics" {
  name               = "${var.name_prefix}-get-range-metrics"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "Booking/revenue totals + daily breakdown for a property or all properties over a date range (max 92 days)"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.tools_lambda_arn

        tool_schema {
          inline_payload {
            name        = "get_range_metrics"
            description = "Get booking/revenue totals and a daily breakdown for a property (or all properties, propertyId=_all) over a date range. Range is capped at 92 days."

            input_schema {
              type        = "object"
              description = "Range metrics request"

              property {
                name        = "propertyId"
                type        = "string"
                description = "Property UUID, or '_all' for chain/region-wide totals"
                required    = true
              }
              property {
                name        = "startDate"
                type        = "string"
                description = "Start date, YYYY-MM-DD"
                required    = true
              }
              property {
                name        = "endDate"
                type        = "string"
                description = "End date, YYYY-MM-DD. Range (inclusive) must not exceed 92 days."
                required    = true
              }
            }
          }
        }
      }
    }
  }

  depends_on = [aws_iam_role_policy.gateway_invoke_tools_lambda, aws_lambda_permission.allow_agentcore_gateway]
}

resource "aws_bedrockagentcore_gateway_target" "get_occupancy" {
  name               = "${var.name_prefix}-get-occupancy"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "Per-property occupancy, revenue, and check-in/out counts for a date range, optionally filtered by region"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.tools_lambda_arn

        tool_schema {
          inline_payload {
            name        = "get_occupancy"
            description = "Get per-property occupancy, revenue, and check-in/out counts for a date range, optionally filtered by region. Region taxonomy: Northeast, Southeast, Midwest, West, South, Other."

            input_schema {
              type        = "object"
              description = "Occupancy request"

              property {
                name        = "startDate"
                type        = "string"
                description = "Start date, YYYY-MM-DD"
                required    = true
              }
              property {
                name        = "endDate"
                type        = "string"
                description = "End date, YYYY-MM-DD"
                required    = true
              }
              property {
                name        = "region"
                type        = "string"
                description = "Optional region filter: Northeast, Southeast, Midwest, West, South, or Other"
                required    = false
              }
            }
          }
        }
      }
    }
  }

  depends_on = [aws_iam_role_policy.gateway_invoke_tools_lambda, aws_lambda_permission.allow_agentcore_gateway]
}

resource "aws_bedrockagentcore_gateway_target" "query_analytics" {
  name               = "${var.name_prefix}-query-analytics"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "Run a fixed, templated analytics query against the sample event lake (read-only). No free-form SQL -- template name + a few scalar arguments only."

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.tools_lambda_arn

        tool_schema {
          inline_payload {
            name        = "query_analytics"
            description = <<-EOT
              Run one of a FIXED set of templated analytics queries against the
              sample event lake (Glue table `events`).
              There is NO free-form SQL -- you choose a template name and supply
              a few scalar arguments; the underlying SQL is fixed and
              parameterized. Templates:
                - event_counts_by_type: counts of each detail_type within one
                  partition-day (requires year, month, day, source_partition).
                - top_properties_by_event_type: which properties generated the
                  most of one event type on one partition-day (requires year,
                  month, day, source_partition, detail_type).
                - daily_event_trend: day-by-day count of one event type across
                  a whole month (requires year, month, source_partition,
                  detail_type).
              Valid source_partition values: crs, pms, billing, loyalty,
              housekeeping, audit, payment. Event types include
              checkinout.checked_in / checked_out (pms), reservation.created /
              cancelled with bookingchannel (crs), housekeeping.room_ready,
              loyalty.points_earned, and payment.refunded.
            EOT

            input_schema {
              type        = "object"
              description = "Templated analytics query request"

              property {
                name        = "template"
                type        = "string"
                description = "One of: event_counts_by_type, top_properties_by_event_type, daily_event_trend"
                required    = true
              }
              property {
                name        = "year"
                type        = "integer"
                description = "Partition year, e.g. 2026"
                required    = true
              }
              property {
                name        = "month"
                type        = "integer"
                description = "Partition month, 1-12"
                required    = true
              }
              property {
                name        = "day"
                type        = "integer"
                description = "Partition day, 1-31. Required for event_counts_by_type and top_properties_by_event_type; omit for daily_event_trend (which spans the whole month)."
                required    = false
              }
              property {
                name        = "source_partition"
                type        = "string"
                description = "One of: crs, pms, billing, loyalty, housekeeping, audit, payment"
                required    = true
              }
              property {
                name        = "detail_type"
                type        = "string"
                description = "Exact detail_type value to filter on (required for top_properties_by_event_type and daily_event_trend, e.g. 'checkinout.checked_out')"
                required    = false
              }
              property {
                name        = "limit"
                type        = "integer"
                description = "Max rows to return, clamped server-side to 50"
                required    = false
              }
            }
          }
        }
      }
    }
  }

  depends_on = [aws_iam_role_policy.gateway_invoke_tools_lambda, aws_lambda_permission.allow_agentcore_gateway]
}

resource "aws_bedrockagentcore_gateway_target" "propose_recommendation" {
  name               = "${var.name_prefix}-propose-recommendation"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "Propose a recommendation for analyst review. Writes a PENDING record only -- record_recommendation must be called separately after explicit confirmation to finalize it."

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.tools_lambda_arn

        tool_schema {
          inline_payload {
            name        = "propose_recommendation"
            description = <<-EOT
              Propose a revenue/pricing recommendation (e.g. a rate change,
              a promotion targeting a soft region) for the analyst to review.
              This writes a PENDING record and returns a recommendationId --
              it does NOT take effect.
              You MUST get the analyst's explicit yes/confirm in the
              conversation, then call record_recommendation with this exact
              recommendationId and confirmed=true to finalize it. Never call
              record_recommendation before the analyst has explicitly said
              yes.
            EOT

            input_schema {
              type        = "object"
              description = "Recommendation proposal"

              property {
                name        = "summary"
                type        = "string"
                description = "One-line summary of the recommended action, e.g. 'Raise weekday rate 5% for West region properties'"
                required    = true
              }
              property {
                name        = "reasoning"
                type        = "string"
                description = "The supporting analysis and data that justifies this recommendation"
                required    = true
              }
              property {
                name        = "supportingData"
                type        = "object"
                description = "Optional structured data backing the recommendation (e.g. the specific figures cited in reasoning)"
                required    = false
              }
            }
          }
        }
      }
    }
  }

  depends_on = [aws_iam_role_policy.gateway_invoke_tools_lambda, aws_lambda_permission.allow_agentcore_gateway]
}

resource "aws_bedrockagentcore_gateway_target" "record_recommendation" {
  name               = "${var.name_prefix}-record-recommendation"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "Finalize a previously PROPOSED recommendation. The only write tool with lasting effect -- can only confirm an existing proposal, never create one from nothing."

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.tools_lambda_arn

        tool_schema {
          inline_payload {
            name        = "record_recommendation"
            description = <<-EOT
              Finalize a recommendation that was already proposed via
              propose_recommendation. ONLY call this AFTER the analyst has
              explicitly confirmed (said yes) in the conversation to the
              proposed recommendation -- never call this speculatively or
              on the analyst's behalf. Calling this with confirmed=false, or
              with a recommendationId that was never proposed, is an error.
              This writes an immutable, timestamped record to storage --
              nothing is written anywhere else.
            EOT

            input_schema {
              type        = "object"
              description = "Recommendation confirmation"

              property {
                name        = "recommendationId"
                type        = "string"
                description = "The exact recommendationId returned by a prior propose_recommendation call"
                required    = true
              }
              property {
                name        = "confirmed"
                type        = "boolean"
                description = "Must be true, and only true after the analyst has explicitly confirmed in conversation"
                required    = true
              }
            }
          }
        }
      }
    }
  }

  depends_on = [aws_iam_role_policy.gateway_invoke_tools_lambda, aws_lambda_permission.allow_agentcore_gateway]
}

resource "aws_bedrockagentcore_gateway_target" "list_properties" {
  name               = "${var.name_prefix}-list-properties"
  gateway_identifier = aws_bedrockagentcore_gateway.main.gateway_id
  description        = "List all properties in the chain with name, city, state, and region"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.tools_lambda_arn

        tool_schema {
          inline_payload {
            name        = "list_properties"
            description = "List all properties in the chain with their name, city, state, and region."

            input_schema {
              type        = "object"
              description = "No parameters required"
            }
          }
        }
      }
    }
  }

  depends_on = [aws_iam_role_policy.gateway_invoke_tools_lambda, aws_lambda_permission.allow_agentcore_gateway]
}
