output "user_pool_id" {
  value = aws_cognito_user_pool.analysts.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.analysts.arn
}

output "spa_client_id" {
  value = aws_cognito_user_pool_client.spa.id
}
