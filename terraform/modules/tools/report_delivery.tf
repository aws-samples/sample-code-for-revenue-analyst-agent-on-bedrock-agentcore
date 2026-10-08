# S3 ObjectCreated (reports/ prefix) -> this same tools
# Lambda (see handler.py::_is_s3_event dispatch) -> pre-signed URL ->
# SES -> the requesting analyst's own email. The email carries a pointer
# only (pre-signed URL + title), never report content or PII (see
# tools/lib/report_delivery.py docstring).
#
# S3 cannot compute a pre-signed URL itself, nor know who to send to, so a
# direct S3->notification subscription would only deliver the raw
# bucket/key JSON -- hence the Lambda-mediated hop in between.
#
# SES (not SNS) because each report goes to whoever requested it. The
# delivery Lambda reads the recipient from the report object's
# `analyst-email` metadata, which generate_report (agent.py) sets from the
# caller's validated token claims.

# SES sender identity. In the SES SANDBOX (the default for a new account),
# BOTH this sender AND every recipient address must be verified, and mail
# only flows to verified addresses -- production use requires a one-time
# SES production-access request (not needed for the sample). Verifying an
# email identity sends a confirmation link to that address; the identity
# is not usable for sending until the link is clicked. This is a one-time
# manual step, so it's created here but the click happens out-of-band.
resource "aws_ses_email_identity" "report_sender" {
  email = var.report_sender_email
}

# Allows S3 to invoke the tools Lambda when a report object is created.
# Scoped via source_arn to THIS ONE bucket, so no other bucket in the
# account can trigger this Lambda.
resource "aws_lambda_permission" "allow_s3_report_created" {
  statement_id   = "AllowS3InvokeOnReportCreated"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.tools.function_name
  principal      = "s3.amazonaws.com"
  source_account = data.aws_caller_identity.current.account_id
  source_arn     = var.artifacts_bucket_arn
}

# The actual bucket notification config is set HERE, not in the
# foundation module that owns the aws_s3_bucket resource itself --
# putting it there would require passing this module's Lambda ARN back
# INTO foundation, but foundation is already a dependency of this module
# (artifacts_bucket_arn, analytics_workgroup_arn), so that would be a
# circular module dependency. aws_s3_bucket_notification only needs the
# bucket's NAME (a plain string this module already receives as
# var.artifacts_bucket_name), not the bucket resource itself, so it's
# safe to declare here.
#
# IMPORTANT: aws_s3_bucket_notification manages a bucket's ENTIRE
# notification configuration, not just this one rule -- if the foundation
# module (or anything else) ever needs to add another notification on
# this same bucket, it must be added to THIS resource, not a second
# aws_s3_bucket_notification resource elsewhere (which would silently
# overwrite this one).
resource "aws_s3_bucket_notification" "artifacts_report_created" {
  bucket = var.artifacts_bucket_name

  lambda_function {
    lambda_function_arn = aws_lambda_function.tools.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "reports/"
    filter_suffix       = ".pdf"
  }

  depends_on = [aws_lambda_permission.allow_s3_report_created]
}
