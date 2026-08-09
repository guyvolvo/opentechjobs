# ---------------------------------------------------------------------------
# API Lambda, invoked via a Function URL (no API Gateway -- see the
# architecture discussion: this is a single JSON API with no need for
# usage-plan API keys or multi-service path routing, so API Gateway would
# only add its per-request cost without buying anything this project uses).
#
# The zip built here is a placeholder containing just api/handler.py at
# infra-apply time. deploy-api.yml (GitHub Actions) is what actually ships
# code changes afterward via `aws lambda update-function-code` -- Terraform
# owns the function's existence and config, not its day-to-day code
# updates, so infra applies don't need to run on every API code change.
# ---------------------------------------------------------------------------

data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../api"
  output_path = "${path.module}/build/api.zip"
  excludes    = ["__pycache__"] # importing handler.py locally to test it (see api/README) leaves this behind
}

resource "aws_iam_role" "api_lambda" {
  name = "${var.project_name}-api-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "api_lambda" {
  name = "${var.project_name}-api-lambda-policy"
  role = aws_iam_role.api_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadJobsDb"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:HeadObject"]
        Resource = "${aws_s3_bucket.data.arn}/jobs.db"
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-api*"
      }
    ]
  })
}

resource "aws_lambda_function" "api" {
  function_name    = "${var.project_name}-api"
  role             = aws_iam_role.api_lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  memory_size      = var.lambda_memory_mb
  timeout          = var.lambda_timeout_s

  environment {
    variables = {
      DATA_BUCKET = aws_s3_bucket.data.bucket
      DATA_KEY    = "jobs.db"
    }
  }
}

resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "NONE" # intentionally public -- the brief's own design point:
  # "the public API is the alerting primitive, anyone can poll it." No paywall, no accounts.

  cors {
    allow_origins = ["*"]
    allow_methods = ["GET"]
    allow_headers = ["content-type"]
    max_age       = 3600
  }
}

resource "aws_cloudwatch_log_group" "api_lambda" {
  name              = "/aws/lambda/${aws_lambda_function.api.function_name}"
  retention_in_days = 14
}
