# Once-daily VACUUM pass, split out of both frequent re-poll cycles on
# 2026-09-08 to get this project's whole AWS bill under a hard
# <$5/month ceiling -- see scrape_maintenance_handler.py's own docstring
# for why VACUUM doesn't belong in a 5-20 minute cycle at all. Mirrors
# scrape_lambda.tf's own resource shapes; kept in its own file for the
# same reason scrape_workday_lambda.tf is: this function's reason for
# existing is genuinely distinct from the rest of the fleet's.

data "archive_file" "scrape_maintenance" {
  type        = "zip"
  source_file = "${path.module}/../scrape_maintenance_handler.py"
  output_path = "${path.module}/build/scrape-maintenance.zip"
}

resource "aws_iam_role" "scrape_maintenance_lambda" {
  name = "${var.project_name}-scrape-maintenance-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scrape_maintenance_lambda" {
  name = "${var.project_name}-scrape-maintenance-lambda-policy"
  role = aws_iam_role.scrape_maintenance_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "JobsDbAndKnownReadWrite"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.data.arn}/jobs.db",
          "${aws_s3_bucket.data.arn}/known.json",
        ]
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-scrape-maintenance*"
      }
    ]
  })
}

resource "aws_lambda_function" "scrape_maintenance" {
  function_name    = "${var.project_name}-scrape-maintenance"
  role             = aws_iam_role.scrape_maintenance_lambda.arn
  handler          = "scrape_maintenance_handler.lambda_handler"
  runtime          = "python3.13"
  filename         = data.archive_file.scrape_maintenance.output_path
  source_code_hash = data.archive_file.scrape_maintenance.output_base64sha256
  memory_size      = var.scrape_maintenance_memory_mb
  timeout          = var.scrape_maintenance_timeout_s

  # VACUUM needs roughly the DB's own size again as scratch space to
  # rebuild it, on top of the already-downloaded original copy -- same
  # reasoning as scrape_fast's pre-sharding ephemeral_storage, kept
  # generous here since this is the one place that cost still applies.
  ephemeral_storage {
    size = 3008
  }

  environment {
    variables = {
      DATA_BUCKET = aws_s3_bucket.data.bucket
    }
  }

  # Same reasoning as scrape_fast's own placeholder -- this archive_file
  # is just scrape_maintenance_handler.py alone, no loader/schema.
  # deploy-scrape-lambda.yml ships the real package.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

resource "aws_cloudwatch_log_group" "scrape_maintenance_lambda" {
  name              = "/aws/lambda/${aws_lambda_function.scrape_maintenance.function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_event_rule" "scrape_maintenance_schedule" {
  name                = "${var.project_name}-scrape-maintenance-schedule"
  description         = "Fires the once-daily jobs.db VACUUM pass"
  schedule_expression = "rate(1 day)"
}

resource "aws_cloudwatch_event_target" "scrape_maintenance_schedule" {
  rule = aws_cloudwatch_event_rule.scrape_maintenance_schedule.name
  arn  = aws_lambda_function.scrape_maintenance.arn
}

resource "aws_lambda_permission" "allow_eventbridge_maintenance" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scrape_maintenance.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.scrape_maintenance_schedule.arn
}
