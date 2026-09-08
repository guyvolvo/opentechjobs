# Workday's own fast re-poll cycle, on its own EventBridge schedule --
# see scrape_workday_handler.py's own docstring for why this is a
# separate function from scrape_fast rather than just removing that
# Lambda's own exclusion of Workday. Mirrors scrape_lambda.tf's own
# resource shapes throughout; kept in a separate file since Workday's
# reasons for existing here are genuinely distinct from the rest of the
# fleet's, not because the pattern itself differs.

data "archive_file" "scrape_workday" {
  type        = "zip"
  source_file = "${path.module}/../scrape_workday_handler.py"
  output_path = "${path.module}/build/scrape-workday.zip"
}

resource "aws_iam_role" "scrape_workday_lambda" {
  name = "${var.project_name}-scrape-workday-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scrape_workday_lambda" {
  name = "${var.project_name}-scrape-workday-lambda-policy"
  role = aws_iam_role.scrape_workday_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # jobs.db/known.json: same conditional-write-safe read-modify-
        # write cycle scrape_fast_lambda's own policy grants -- see
        # load_to_sqlite.py's s3_push_conditional. status.json: this
        # Lambda's own real-time phase (scrape_workday_handler.py's
        # _write_status), same file scrape_fast_lambda writes too.
        Sid    = "JobsDbAndKnownReadWrite"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.data.arn}/jobs.db",
          "${aws_s3_bucket.data.arn}/known.json",
          "${aws_s3_bucket.data.arn}/status.json",
        ]
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-scrape-workday*"
      }
    ]
  })
}

resource "aws_lambda_function" "scrape_workday" {
  function_name    = "${var.project_name}-scrape-workday"
  role             = aws_iam_role.scrape_workday_lambda.arn
  handler          = "scrape_workday_handler.lambda_handler"
  runtime          = "python3.13"
  filename         = data.archive_file.scrape_workday.output_path
  source_code_hash = data.archive_file.scrape_workday.output_base64sha256
  memory_size      = var.scrape_workday_memory_mb
  timeout          = var.scrape_workday_timeout_s

  # Was 1024 the same day: reasoned that skipping VACUUM (no more
  # ~2x-DB-size scratch need) meant this only had to hold the
  # downloaded jobs.db itself -- true, but jobs.db passed 795MB a few
  # hours later, leaving under 230MB of real margin for everything
  # else in /tmp. Bumped to 3008 (this Lambda's own OOM was the
  # memory ceiling, not ephemeral storage, but both were sized against
  # the same now-stale assumption -- fixing one without the other just
  # moves the next wall here instead).
  ephemeral_storage {
    size = 3008
  }

  environment {
    variables = {
      DATA_BUCKET = aws_s3_bucket.data.bucket
    }
  }

  # Same reasoning as scrape_fast's own placeholder -- this archive_file
  # is just scrape_workday_handler.py alone, no probe.py/companies.yml/
  # loader/requests (Terraform's archive_file can't run pip or bundle a
  # multi-file package the way deploy-scrape-lambda.yml does). Without
  # this, a later `terraform apply` would silently overwrite the real
  # deployed code with a build missing everything probe.py needs,
  # breaking every scheduled run until the next code push.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

resource "aws_cloudwatch_log_group" "scrape_workday_lambda" {
  name              = "/aws/lambda/${aws_lambda_function.scrape_workday.function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_event_rule" "scrape_workday_schedule" {
  name        = "${var.project_name}-scrape-workday-schedule"
  description = "Fires the Workday-only fast re-poll Lambda"
  # Was 5 minutes. Loosened to 20 on 2026-09-08 as part of getting this
  # project's whole AWS bill under a hard <$5/month ceiling -- unlike
  # scrape_fast, this function isn't sharded (its company set is small
  # and hand-pinned, not the thing driving unbounded growth), so its
  # only cost lever is frequency. 20 minutes still catches a "posted
  # today" transition to a real date same-day, just not within minutes
  # of it happening -- an acceptable trade for a company set this small
  # and non-time-critical.
  schedule_expression = "rate(20 minutes)"
}

resource "aws_cloudwatch_event_target" "scrape_workday_schedule" {
  rule = aws_cloudwatch_event_rule.scrape_workday_schedule.name
  arn  = aws_lambda_function.scrape_workday.arn
}

resource "aws_lambda_permission" "allow_eventbridge_workday" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scrape_workday.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.scrape_workday_schedule.arn
}
