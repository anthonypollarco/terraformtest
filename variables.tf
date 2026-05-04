variable "aws_region" {
  description = "AWS region where resources will be created"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix used for naming resources"
  type        = string
  default     = "tenable"
}

variable "s3_bucket_name" {
  description = "Name of bucket used for Tenable export files"
  type        = string
}

variable "tenable_secret_arn" {
  description = "ARN of Secrets Manager secret containing Tenable API keys as JSON: {\"accessKey\":\"...\",\"secretKey\":\"...\"}"
  type        = string
}

variable "lambda_layer_arns" {
  description = "Optional Lambda layer ARNs (for example, a layer containing requests)"
  type        = list(string)
  default     = []
}

variable "requests_layer_arn" {
  description = "Lambda layer ARN that packages the Python requests dependency used by lambda_function.py"
  type        = string
  default     = null
}
