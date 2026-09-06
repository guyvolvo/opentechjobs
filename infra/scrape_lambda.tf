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
        # Reported live: every alert digest failed with AccessDenied on
        # 'ses:SendEmail' against the *recipient's* identity ARN, not
        # the sender's -- this account is still in the SES sandbox
        # (confirmed via `aws sesv2 get-account`), and sandbox mode
        # requires IAM authorization against both identities in a send,
        # not just the verified sending domain. Alert recipients are
        # arbitrary Cognito users' own email addresses, impossible to
        # enumerate as fixed Resource ARNs ahead of time -- Resource:*
        # is the standard pattern for exactly this shape. Not a
        # broadened blast radius in practice: the action itself only
        # ever sends mail, and SES's own identity verification (plus
        # the sandbox's verified-recipient requirement, until
        # production access is granted) remains the real boundary on
        # what can actually go out, regardless of what this policy allows.
        Sid      = "SendAlertDigests"
        Effect   = "Allow"
        Action   = ["ses:SendEmail"]
        Resource = "*"
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
  # Tried reserved_concurrent_executions = 1 here (a run pulls jobs.db
  # from S3, upserts, pushes it back -- not atomic, so two overlapping
  # invocations could race and silently drop one's updates). Rejected
  # live: this AWS account's total Lambda concurrency quota is low
  # enough (looks capped around 10-11) that reserving even 1 for a
  # single function violates AWS's own enforced 10-unreserved minimum
  # account-wide. Would need a Service Quotas increase request first,
  # not something Terraform can route around. Left unset for now --
  # the 5-minute schedule below still has real margin (a full run
  # measured 103.85s, well under 300s) even without this belt-and-
  # suspenders protection, just without the hard guarantee.

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
  # this 300s window even before the memory bump and WORKERS increase
  # above. No reserved-concurrency guard against two runs overlapping
  # (see the function resource's own comment on why not), so this margin
  # is what's actually keeping runs from ever colliding -- not a hard
  # guarantee, revisit if real Duration ever creeps close to 300s.
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
