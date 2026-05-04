output "lambda_function_name" {
  value = aws_lambda_function.tenable_export.function_name
}

output "eventbridge_rule_name" {
  value = aws_cloudwatch_event_rule.daily_midnight.name
}

output "s3_bucket_name" {
  value = aws_s3_bucket.exports.bucket
}
