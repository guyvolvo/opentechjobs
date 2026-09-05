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
        # alerts.py: full scan + per-alert watermark update, run once
        # per fast-poll cycle after the loader step above.
        Sid      = "EvaluateAlerts"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.alerts.arn
      },
      {
        Sid      = "SendAlertDigests"
        Effect   = "Allow"
        Action   = ["ses:SendEmail"]
        Resource = aws_ses_domain_identity.main.arn
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
  # A run pulls the current jobs.db from S3, upserts into it, pushes it
  # back -- not an atomic/locked operation. Two overlapping invocations
  # (a slow run still going when the next scheduled one fires) would race
  # on that read-modify-write and could silently drop whichever one
  # finished writing first. Reserved at 1 so EventBridge's own retry
  # mechanism absorbs a slow cycle (queues/retries) instead of ever
  # letting two runs touch jobs.db at the same time. Matters more now that
  # a shorter schedule (see the event rule below) is being considered
  # specifically because runs got faster, not because they're guaranteed
  # short.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      DATA_BUCKET       = aws_s3_bucket.data.bucket
      ALERTS_TABLE      = aws_dynamodb_table.alerts.name
      ALERTS_FROM_EMAIL = var.alerts_from_email
      SITE_ORIGIN       = "https://${var.domain_name}"
    }
  }

  # Unlike the API Lambda's placeholder (which zips the whole real api/
  # dir, so it's functionally equivalent to what deploy-api.yml ships),
  # this one is genuinely incomplete -- just scrape_handler.py, no
  # probe.py/loader/requests, since Terraform's archive_file can't run
  # pip. A later `terraform apply` picking up on that hash difference
  # would silently overwrite deploy-scrape-lambda.yml's real deployed
  # code with a build missing probe.py entirely, breaking every
  # scheduled run until the next code push -- happened once already
  # (2026-09-04). ignore_changes makes code exclusively the deploy
  # workflow's, matching the intent above, not just the placeholder
  # zip's initial-create purpose.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

resource "aws_cloudwatch_log_group" "scrape_fast_lambda" {
  name              = "/aws/lambda/${aws_lambda_function.scrape_fast.function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_event_rule" "scrape_fast_schedule" {
  name        = "${var.project_name}-scrape-fast-schedule"
  description = "Fires the fast re-poll Lambda every 5 minutes"
  # Was 10 minutes. Tightened once, not blindly: a full run (Workday
  # excluded, WORKERS still 8) measured 103.85s live -- comfortably under
  # this 300s window even before the memory/concurrency bumps above.
  # reserved_concurrent_executions=1 on the function itself is the real
  # guard against two runs ever overlapping, not just this number being
  # generous; that's what actually made shortening the interval safe.
  schedule_expression = "rate(5 minutes)"
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
