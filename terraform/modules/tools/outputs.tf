output "lambda_function_name" {
  description = "Name of the single tools Lambda backing all AgentCore Gateway tool targets."
  value       = aws_lambda_function.tools.function_name
}

output "lambda_function_arn" {
  description = "ARN of the tools Lambda."
  value       = aws_lambda_function.tools.arn
}

output "lambda_execution_role_arn" {
  description = "ARN of the tools Lambda's execution role."
  value       = aws_iam_role.tools_lambda.arn
}

output "report_sender_identity_arn" {
  description = "ARN of the SES email identity used as the From address for pointer-only (pre-signed URL) report-ready emails. Must be verified before sending; in the SES sandbox recipients must also be verified."
  value       = aws_ses_email_identity.report_sender.arn
}
