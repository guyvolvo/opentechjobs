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
        # Partition & Merge (2026-09-08): writes its own pinned
        # jobs-partition-workday.db instead of the shared jobs.db -- see
        # load_to_sqlite.py's --key and --skip-known. Never actually read
        # known.json (companies.yml/PINS drives this Lambda instead), so
        # that grant is dropped here, not just left unused. jobs.db is
        # left in the grant, unused, as a rollback path during the
        # cutover -- drop once jobs-read.db has been live and verified.
        # status.json: this Lambda's own real-time phase
        # (scrape_workday_handler.py's _write_status), same file
        # scrape_fast_lambda writes too.
        Sid    = "PartitionsAndStatusReadWrite"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.data.arn}/jobs.db",
          "${aws_s3_bucket.data.arn}/jobs-partition-*",
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
  # Was 5, then 20, then 120, then 240 minutes, all the same day
  # (2026-09-08) -- 240 alongside turning on FETCH_FULL_DESCRIPTIONS
  # unconditionally (fixes "most Workday listings have no description,"
  # at the cost of a per-job detail fetch that used to be conditional).
  # Tightened back to 10 the same day, once known_external_ids-gated
  # descriptions (see scrape_workday_handler.py's own docstring) made
  # the real cost driver "new/changed jobs only," not "every open job,
  # every cycle": confirmed live against nvidia.com (60 jobs, an
  # unusually high 40% multi-location share) that a steady-state cycle
  # (nothing new) ran 3.7x faster than a from-scratch one -- 6.3s vs
  # 23.2s for that one company alone. 10, not the low end of the 5-10
  # target range: only nvidia.com got real per-company timing measured
  # before this shipped, not all 12 pins together under real Lambda
  # concurrency -- watch actual CloudWatch Duration across a few real
  # cycles before tightening to 5, same discipline as scrape-fast's own
  # cadence restoration.
  # 30 minutes, up from 10 (2026-09-09). Measured: this Lambda was 61%
  # of the project's ENTIRE Lambda bill (~780,000 GB-s/month of ~1.27M)
  # to re-poll twelve hand-pinned tenants -- it never got sharded like
  # scrape_fast, so every invocation walks all of companies.yml's workday
  # pins, 144 times a day at ~61.5s each. Those twelve are Intel, Cisco,
  # NVIDIA, Salesforce, PayPal, Visa and the like: enterprise boards whose
  # Israel-facing postings move on a scale of days, so a 10-minute cadence
  # bought no freshness anyone could observe. Frees ~200,000 GB-s/month
  # for the shard rotation, where churn actually happens.
  schedule_expression = "rate(30 minutes)"
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
