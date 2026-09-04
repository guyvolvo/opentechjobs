# Runs the fast re-poll cycle on an EventBridge schedule, replacing
# scrape-fast.yml's GitHub Actions schedule: trigger -- see
# scrape_handler.py's own docstring for why that had to go.
#
# The zip built here is a placeholder containing just scrape_handler.py at
# infra-apply time, no probe.py/loader/requests. deploy-scrape-lambda.yml
# (GitHub Actions) ships the real package via `aws lambda update-function-code`,
# same split as api.tf.

data "archive_file" "scrape_fast" {
  type        = "zip"
  source_file = "${path.module}/../scrape_handler.py"
  output_path = "${path.module}/build/scrape-fast.zip"
}

resource "aws_iam_role" "scrape_fast_lambda" {
  name = "${var.project_name}-scrape-fast-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scrape_fast_lambda" {
  name = "${var.project_name}-scrape-fast-lambda-policy"
  role = aws_iam_role.scrape_fast_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "JobsDbAndKnownReadWrite"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.data.arn}/jobs.db",
          "${aws_s3_bucket.data.arn}/known.json",
        ]
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-scrape-fast*"
      }
    ]
  })
}

resource "aws_lambda_function" "scrape_fast" {
  function_name    = "${var.project_name}-scrape-fast"
  role             = aws_iam_role.scrape_fast_lambda.arn
  handler          = "scrape_handler.lambda_handler"
  runtime          = "python3.13"
  filename         = data.archive_file.scrape_fast.output_path
  source_code_hash = data.archive_file.scrape_fast.output_base64sha256
  memory_size      = var.scrape_lambda_memory_mb
  timeout          = var.scrape_lambda_timeout_s

  environment {
    variables = {
      DATA_BUCKET = aws_s3_bucket.data.bucket
    }
  }
}

resource "aws_cloudwatch_log_group" "scrape_fast_lambda" {
  name              = "/aws/lambda/${aws_lambda_function.scrape_fast.function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_event_rule" "scrape_fast_schedule" {
  name                = "${var.project_name}-scrape-fast-schedule"
  description         = "Fires the fast re-poll Lambda every 10 minutes"
  schedule_expression = "rate(10 minutes)"
}

resource "aws_cloudwatch_event_target" "scrape_fast_schedule" {
  rule = aws_cloudwatch_event_rule.scrape_fast_schedule.name
  arn  = aws_lambda_function.scrape_fast.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scrape_fast.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.scrape_fast_schedule.arn
}
