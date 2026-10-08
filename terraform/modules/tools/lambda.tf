# Single Lambda backing every AgentCore Gateway tool target: the three
# read-only reporting tools, query_analytics, and the two recommendation
# tools. One function with an internal router keeps the sample small; split
# it per tool if you need separate scaling or IAM boundaries.
# generate_report is not here -- see the PresignReportsForDelivery note.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "archive_file" "tools_lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../../../tools"
  output_path = "${path.module}/build/tools_lambda.zip"
  excludes    = ["__pycache__", ".gitkeep", "requirements.txt"]
}

# Lambda layer with PyJWT + cryptography, used by lib/token_verifier.py to
# verify the analyst's Cognito ID token against the user pool's JWKS. Built
# locally by scripts/build_tools_layer.sh (Linux x86_64 wheels for Python
# 3.13, no Docker). Rebuilt whenever tools/requirements.txt changes.
resource "null_resource" "build_tools_layer" {
  triggers = {
    requirements_hash = filesha256("${path.module}/../../../tools/requirements.txt")
    build_script_hash = filesha256("${path.module}/../../../scripts/build_tools_layer.sh")
  }

  provisioner "local-exec" {
    command = "${path.module}/../../../scripts/build_tools_layer.sh"
  }
}

data "archive_file" "tools_layer" {
  type        = "zip"
  source_dir  = "${path.module}/build/tools-layer"
  output_path = "${path.module}/build/tools_layer.zip"
  excludes    = ["__pycache__"]

  depends_on = [null_resource.build_tools_layer]
}

resource "aws_lambda_layer_version" "tools_deps" {
  layer_name               = "${var.name_prefix}-tools-deps"
  description              = "PyJWT and cryptography for Cognito token verification"
  filename                 = data.archive_file.tools_layer.output_path
  source_code_hash         = data.archive_file.tools_layer.output_base64sha256
  compatible_runtimes      = ["python3.13"]
  compatible_architectures = ["x86_64"]
}

resource "aws_cloudwatch_log_group" "tools_lambda" {
  name              = "/aws/lambda/${var.name_prefix}-tools"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "tools_lambda" {
  name = "${var.name_prefix}-tools-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "tools_lambda" {
  name = "${var.name_prefix}-tools-lambda-policy"
  role = aws_iam_role.tools_lambda.id

  # Least privilege: each statement is scoped to the one resource or
  # prefix the matching tool needs.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Logs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.tools_lambda.arn}:*"
      },
      {
        Sid    = "XRayTracing"
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords"
        ]
        Resource = "*"
      },
      {
        # query_analytics: read-only Glue metadata lookups, which Athena
        # needs to resolve the events table. Scoped to this one database
        # and table.
        Sid    = "GlueCatalogReadOnly"
        Effect = "Allow"
        Action = [
          "glue:GetTable",
          "glue:GetDatabase",
          "glue:GetPartitions"
        ]
        Resource = [
          "arn:aws:glue:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:database/${var.analytics_glue_database}",
          "arn:aws:glue:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${var.analytics_glue_database}/${var.analytics_glue_table}"
        ]
      },
      {
        # Athena calls scoped to this sample's workgroup, which enforces
        # the per-query scan cap (see ../foundation/athena.tf).
        Sid    = "AthenaQueryOwnWorkgroup"
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution",
          "athena:GetQueryExecution",
          "athena:GetQueryResults",
          "athena:StopQueryExecution"
        ]
        Resource = var.analytics_workgroup_arn
      },
      {
        # Read the sample event files. Read-only: no write or delete.
        Sid    = "ReadAnalyticsEventsBucket"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          var.analytics_bucket_arn,
          "${var.analytics_bucket_arn}/*"
        ]
      },
      {
        # Athena writes query results to the artifacts bucket
        # (athena-results/ prefix). ListBucket is bucket-level (per S3's own permission model), so
        # it's scoped separately below with a prefix condition rather than
        # combined with the object-level actions here.
        Sid    = "WriteAthenaResultsToOwnBucketObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = "${var.artifacts_bucket_arn}/athena-results/*"
      },
      {
        Sid      = "ListOwnBucketAthenaResultsPrefix"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = var.artifacts_bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = "athena-results/*"
          }
        }
      },
      {
        # Athena's "verify/create output bucket" preflight check (run
        # before every StartQueryExecution) requires s3:GetBucketLocation
        # on the bucket resource itself. Without it queries fail with
        # "Unable to verify/create output bucket". Kept as
        # its own statement (not combined with the prefix-conditioned
        # ListBucket above) because s3:prefix has no meaning for
        # GetBucketLocation -- combining them would make IAM evaluate the
        # condition as unmet and deny GetBucketLocation entirely.
        # GetBucketLocation only reveals the bucket's region, not its
        # contents, so bucket-level scope here doesn't widen blast radius.
        Sid      = "GetOwnBucketLocationForAthena"
        Effect   = "Allow"
        Action   = "s3:GetBucketLocation"
        Resource = var.artifacts_bucket_arn
      },
      {
        # report_delivery.py: generates a short-lived pre-signed GET URL
        # for each new report object (S3 ObjectCreated trigger). This
        # Lambda does NOT write reports; generate_report is a local agent
        # tool that writes with the Runtime's own role (see
        # ../agent/runtime.tf, GenerateReportWriteAccess).
        # This Lambda only ever READS report objects: head_object (to read
        # the analyst-email metadata that resolves the recipient) and
        # generate_presigned_url both require s3:GetObject -- HeadObject is
        # authorized by the GetObject permission, so this one action covers
        # both, no separate s3:HeadObject grant needed.
        Sid      = "PresignReportsForDelivery"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${var.artifacts_bucket_arn}/reports/*"
      },
      {
        # Sends the pointer-only report email (pre-signed URL + title,
        # never report content) to the requesting analyst via SES.
        # Constrained to the one verified sender identity, so this role cannot send from arbitrary
        # addresses even if the code were changed to try.
        Sid      = "SendReportReadyEmail"
        Effect   = "Allow"
        Action   = "ses:SendEmail"
        Resource = "*"
        Condition = {
          StringEquals = {
            "ses:FromAddress" = var.report_sender_email
          }
        }
      },
      {
        # The write tools. propose_recommendation
        # writes PENDING records; record_recommendation transitions an
        # EXISTING PENDING record to CONFIRMED (see
        # tools/lib/recommendation_client.py for why this two-step design
        # is the real, server-side human-in-the-loop gate). Both need
        # read+write on this one prefix of the artifacts bucket only.
        # GetObject is required so record_recommendation can
        # read back the PENDING record it's confirming.
        Sid    = "RecommendationReadWrite"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = "${var.artifacts_bucket_arn}/recommendations/*"
      },
      {
        # Without s3:ListBucket, S3
        # cannot disclose whether GetObject failed because the key doesn't
        # exist or because access is denied, so it returns a generic 403
        # AccessDenied for a nonexistent recommendation ID instead of 404
        # NoSuchKey -- this is documented S3 behavior (see AWS: "Amazon S3
        # will return... AccessDenied" when ListBucket is absent), not a
        # bug in the application logic. The SECURITY property already held
        # without this (a fake recommendationId was correctly rejected),
        # but the error code was misleading. Same prefix-conditioned
        # pattern already used for athena-results/.
        Sid      = "ListOwnBucketRecommendationsPrefix"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = var.artifacts_bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = "recommendations/*"
          }
        }
      }
    ]
  })
}

resource "aws_lambda_function" "tools" {
  function_name = "${var.name_prefix}-tools"
  role          = aws_iam_role.tools_lambda.arn
  # source_dir zips the CONTENTS of tools/ to the zip root -- handler.py
  # and lib/ land at the top level, not under a `tools/` package. So the
  # entry point and internal imports use `handler` / `lib.*`, not
  # `tools.handler` / `tools.lib.*`. tests/conftest.py mirrors this exact
  # layout so pytest exercises the same import structure that's deployed.
  handler       = "handler.lambda_handler"
  runtime       = "python3.13"
  architectures = ["x86_64"] # must match the layer's wheels (build_tools_layer.sh)
  layers        = [aws_lambda_layer_version.tools_deps.arn]
  timeout       = 30
  memory_size   = 256
  # Caps concurrent invocations so a runaway agent loop cannot run up cost
  # or starve other functions in the account. Raise for real traffic.
  reserved_concurrent_executions = 5
  filename                       = data.archive_file.tools_lambda.output_path
  source_code_hash               = data.archive_file.tools_lambda.output_base64sha256

  # Traces this Lambda's
  # invocations in X-Ray so tool-call latency/errors are visible alongside
  # the agent's own spans. 
  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      ANALYTICS_GLUE_DATABASE  = var.analytics_glue_database
      ANALYTICS_WORKGROUP_NAME = var.analytics_workgroup_name
      ARTIFACTS_BUCKET_NAME    = var.artifacts_bucket_name
      # SES report delivery (see report_delivery.tf). Sender = the verified
      # SES identity. Default recipient is used only when a report object
      # has no analyst-email metadata (a token without an email claim), so
      # a report is never dropped silently.
      REPORT_SENDER_EMAIL            = var.report_sender_email
      REPORT_DEFAULT_RECIPIENT_EMAIL = var.report_notification_email
      # Cognito ID token verification (lib/token_verifier.py). The Lambda
      # checks signature, issuer, audience, token_use, and expiry itself
      # instead of trusting the upstream authorizer.
      COGNITO_REGION        = data.aws_region.current.region
      COGNITO_USER_POOL_ID  = var.cognito_user_pool_id
      COGNITO_APP_CLIENT_ID = var.cognito_app_client_id
    }
  }

  depends_on = [aws_cloudwatch_log_group.tools_lambda]

  tags = {
    Name = "${var.name_prefix}-tools"
  }
}

# The resource-based permission that lets AgentCore Gateway invoke this
# Lambda lives in the agent module (terraform/modules/agent/gateway.tf), next
# to the Gateway it is scoped to. It needs the Gateway's ARN as source_arn,
# and the agent module already depends on this module for the Lambda ARN.
