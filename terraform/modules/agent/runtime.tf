# AgentCore Runtime (direct code deployment -- no container/ECR, see
# s3.tf for why). The Runtime executes agent/agent.py directly on a
# managed Amazon Linux arm64 Python 3.13 environment, using the S3 zip
# built by scripts/build_agent_zip.sh.

resource "aws_cloudwatch_log_group" "agent_runtime" {
  name              = "/aws/bedrock-agentcore/runtimes/${var.name_prefix}-agent"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "agent_runtime" {
  name = "${var.name_prefix}-agent-runtime-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AssumeRolePolicy"
        Effect = "Allow"
        Principal = {
          Service = "bedrock-agentcore.amazonaws.com"
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
          }
        }
      }
    ]
  })

  tags = {
    Name = "${var.name_prefix}-agent-runtime-role"
  }
}

resource "aws_iam_role_policy" "agent_runtime" {
  name = "${var.name_prefix}-agent-runtime-policy"
  role = aws_iam_role.agent_runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadDeploymentPackage"
        Effect = "Allow"
        Action = [
          "s3:GetObject"
        ]
        Resource = "${aws_s3_bucket.agent_code.arn}/*"
      },
      {
        # AgentCore Runtime auto-creates its own log group per agent/
        # endpoint (naming convention outside our control, not necessarily
        # matching the log group we pre-created above for reference/manual
        # use) -- scope broadly to the runtimes/* namespace rather than one
        # specific log group ARN, so the actual auto-created group isn't
        # silently denied.
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogStreams",
          "logs:CreateLogGroup",
          "logs:DescribeLogGroups",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
      },
      {
        Sid    = "XRayTracing"
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets"
        ]
        Resource = "*"
      },
      {
        Sid      = "CloudWatchMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "bedrock-agentcore"
          }
        }
      },
      {
        # Multi-model Claude strategy (Haiku routes, Sonnet reasons, Opus
        # for heavy analysis). Cross-region "us." inference profiles can
        # route the underlying InvokeModel call to ANY US region (observed:
        # us-east-1 profile routed to a us-east-2 foundation model) -- so
        # the foundation-model resource must NOT be pinned to one region.
        # Still scoped to Anthropic Claude models only (not "*" across all
        # providers), and the inference-profile ARNs remain scoped to
        # exactly the three pinned profiles this agent uses.
        Sid    = "BedrockModelInvocation"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/anthropic.*",
          "arn:aws:bedrock:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.agent_haiku_model_id}",
          "arn:aws:bedrock:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.agent_sonnet_model_id}",
          "arn:aws:bedrock:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.agent_opus_model_id}"
        ]
      },
      {
        Sid    = "GetAgentAccessToken"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetWorkloadAccessTokenForUserId"
        ]
        Resource = [
          "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/*"
        ]
      },
      {
        # Lets the Runtime's own execution role SigV4-sign requests to
        # AgentCore Gateway (see agent.py: aws_iam_streamablehttp_client,
        # aws_service="bedrock-agentcore"). Scoped to this one gateway.
        Sid      = "InvokeGateway"
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeGateway"
        Resource = aws_bedrockagentcore_gateway.main.gateway_arn
      },
      {
        # Required for BedrockModel(guardrail_id=..., guardrail_version=...)
        # in agent.py -- applies the guardrail on every model invocation.
        Sid      = "ApplyGuardrail"
        Effect   = "Allow"
        Action   = "bedrock:ApplyGuardrail"
        Resource = aws_bedrock_guardrail.main.guardrail_arn
      },
      {
        # AgentCoreMemorySessionManager (agent.py) reads/writes short-term
        # conversation events and retrieves long-term semantic records --
        # scoped to this one memory resource only.
        Sid    = "MemoryAccess"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
          # DeleteEvent is required: when
          # the Guardrail redacts a blocked/off-scope message,
          # AgentCoreMemorySessionManager's redact_latest_message ->
          # update_message calls DeleteEvent internally to replace the
          # old (unredacted) event with a new one. Without this, ANY
          # Guardrail-blocked turn crashes the whole invocation with a
          # SessionException, rather than just returning the refusal.
          "bedrock-agentcore:DeleteEvent",
          "bedrock-agentcore:ListSessions",
          "bedrock-agentcore:ListActors",
          "bedrock-agentcore:RetrieveMemoryRecords",
          "bedrock-agentcore:GetMemoryRecord",
          "bedrock-agentcore:ListMemoryRecords"
        ]
        Resource = [
          aws_bedrockagentcore_memory.main.arn,
          "${aws_bedrockagentcore_memory.main.arn}/*"
        ]
      },
      {
        # Code Interpreter -- sandboxed Python for WoW deltas,
        # per-property contribution analysis, cancellation correlation,
        # etc. Arithmetic happens in the sandbox, never in the model's
        # head. Uses AWS's managed default identifier
        # (aws.codeinterpreter.v1, PUBLIC network mode) rather than a
        # custom aws_bedrockagentcore_code_interpreter resource -- this
        # agent's use case is pure Python computation on data already
        # fetched via tool calls, no VPC/S3/EFS file mounting needed, so a
        # custom resource would add setup complexity with no benefit.
        # Scoped to that one well-known AWS-owned identifier, not "*".
        Sid    = "CodeInterpreterAccess"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:StartCodeInterpreterSession",
          "bedrock-agentcore:InvokeCodeInterpreter",
          "bedrock-agentcore:StopCodeInterpreterSession",
          "bedrock-agentcore:GetCodeInterpreterSession",
          "bedrock-agentcore:ListCodeInterpreterSessions"
        ]
        Resource = "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:aws:code-interpreter/aws.codeinterpreter.v1"
      },
      {
        # generate_report is a local tool in agent.py (see gateway.tf for
        # why). This lets the Runtime write the finished PDF straight to
        # S3, scoped to the reports/ prefix of the artifacts bucket.
        Sid      = "GenerateReportWriteAccess"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${var.artifacts_bucket_arn}/reports/*"
      }
    ]
  })
}

resource "aws_bedrockagentcore_agent_runtime" "main" {
  agent_runtime_name = replace("${var.name_prefix}_agent", "-", "_")
  description        = "Revenue / Occupancy Analyst Agent (Strands, direct code deployment)"
  role_arn           = aws_iam_role.agent_runtime.arn

  agent_runtime_artifact {
    code_configuration {
      entry_point = ["agent.py"]
      runtime     = "PYTHON_3_13"
      code {
        s3 {
          bucket = aws_s3_bucket.agent_code.id
          prefix = aws_s3_object.agent_code.key
        }
      }
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  # Inbound auth: the Runtime validates the analyst's Cognito ID token
  # (issuer, signature, expiry, audience) before agent.py runs. agent.py
  # then decodes the already-validated claims without re-verifying, which
  # is the pattern AWS documents for AgentCore Runtime OAuth.
  #
  # Cognito ID tokens carry `aud` but no `client_id` claim, so
  # allowed_audience is the right field here; allowed_clients checks
  # client_id, which only access tokens have.
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = "https://cognito-idp.${data.aws_region.current.region}.amazonaws.com/${var.user_pool_id}/.well-known/openid-configuration"
      allowed_audience = [var.spa_client_id]
    }
  }

  # The validated Authorization header only reaches agent.py's
  # context.request_headers if it is also allowlisted here. Without this,
  # the authorizer still gates access, but agent.py never sees the claims,
  # so per-user memory and access scoping stop working.
  request_header_configuration {
    request_header_allowlist = ["Authorization"]
  }

  environment_variables = {
    AGENTCORE_GATEWAY_URL = aws_bedrockagentcore_gateway.main.gateway_url
    AGENT_HAIKU_MODEL_ID  = var.agent_haiku_model_id
    AGENT_SONNET_MODEL_ID = var.agent_sonnet_model_id
    AGENT_OPUS_MODEL_ID   = var.agent_opus_model_id
    GUARDRAIL_ID          = aws_bedrock_guardrail.main.guardrail_id
    GUARDRAIL_VERSION     = aws_bedrock_guardrail_version.main.version
    AGENTCORE_MEMORY_ID   = aws_bedrockagentcore_memory.main.id
    ARTIFACTS_BUCKET_NAME = var.artifacts_bucket_name
  }

  depends_on = [
    aws_cloudwatch_log_group.agent_runtime,
    aws_iam_role_policy.agent_runtime,
    aws_bedrockagentcore_gateway_target.get_range_metrics,
    aws_bedrockagentcore_gateway_target.get_occupancy,
    aws_bedrockagentcore_gateway_target.list_properties,
    aws_bedrockagentcore_gateway_target.query_analytics,
    aws_bedrockagentcore_gateway_target.propose_recommendation,
    aws_bedrockagentcore_gateway_target.record_recommendation
  ]
}
