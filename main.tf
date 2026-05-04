terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name_prefix = var.project_name
}

resource "aws_s3_bucket" "exports" {
  bucket = var.s3_bucket_name
}

resource "aws_s3_bucket_lifecycle_configuration" "exports_lifecycle" {
  bucket = aws_s3_bucket.exports.id

  rule {
    id     = "expire-objects-30-days"
    status = "Enabled"

    expiration {
      days = 30
    }
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name_prefix}-tenable-export"
  retention_in_days = 30
}

resource "aws_iam_role" "lambda_exec" {
  name = "${local.name_prefix}-tenable-export-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "${local.name_prefix}-tenable-export-policy"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.exports.arn,
          "${aws_s3_bucket.exports.arn}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = var.tenable_secret_arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_function.py"
  output_path = "${path.module}/lambda_function.zip"
}

resource "aws_lambda_function" "tenable_export" {
  function_name    = "${local.name_prefix}-tenable-export"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  timeout          = 900
  memory_size      = 512
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  layers           = var.lambda_layer_arns

  environment {
    variables = {
      TENABLE_SECRET_ARN = var.tenable_secret_arn
      OUTPUT_BUCKET      = aws_s3_bucket.exports.bucket
      OUTPUT_PREFIX      = "tenable-exports"
      CHECKPOINT_KEY     = "state/last_sync.txt"
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_cloudwatch_event_rule" "daily_midnight" {
  name                = "${local.name_prefix}-tenable-daily"
  description         = "Run Tenable vulnerability export daily at 12:01 AM CST (06:01 UTC)"
  schedule_expression = "cron(1 6 * * ? *)"
}

resource "aws_cloudwatch_event_target" "lambda_target" {
  rule      = aws_cloudwatch_event_rule.daily_midnight.name
  target_id = "tenable-export-lambda"
  arn       = aws_lambda_function.tenable_export.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tenable_export.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_midnight.arn
}
