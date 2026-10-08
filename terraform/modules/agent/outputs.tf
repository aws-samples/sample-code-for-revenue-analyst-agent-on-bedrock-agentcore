output "agent_runtime_id" {
  description = "ID of the agent runtime."
  value       = aws_bedrockagentcore_agent_runtime.main.agent_runtime_id
}

output "agent_runtime_arn" {
  description = "ARN of the agent runtime."
  value       = aws_bedrockagentcore_agent_runtime.main.agent_runtime_arn
}

output "gateway_id" {
  description = "ID of the AgentCore Gateway."
  value       = aws_bedrockagentcore_gateway.main.gateway_id
}

output "gateway_url" {
  description = "URL endpoint of the AgentCore Gateway."
  value       = aws_bedrockagentcore_gateway.main.gateway_url
}

output "agent_code_bucket_name" {
  description = "S3 bucket holding the agent's direct-code-deployment zip."
  value       = aws_s3_bucket.agent_code.id
}
