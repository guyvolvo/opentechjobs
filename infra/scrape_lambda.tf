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
        # Without ListBucket, S3 answers 403 AccessDenied rather than 404
        # for a key that does not exist yet, which is how every
        # partition's first write died silently. See s3_pull, which now
        # also treats that 403 as absent. Scoped by prefix, so this still
        # cannot enumerate the rest of the bucket.
        Sid      = "ListOwnPrefixes"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.data.arn
        Condition = {
          StringLike = { "s3:prefix" = ["jobs-partition-*", "descriptions/*"] }
        }
      },
      {
        # Partition & Merge (2026-09-08): this Lambda now writes its own
        # shard's jobs-partition-{N}.db instead of the shared jobs.db --
        # see load_to_sqlite.py's --key and --skip-known, scrape_handler.py's
        # own docstring. known.json stays read-only here (this Lambda still
        # downloads it directly to pick a shard; --skip-known means it never
        # writes it back). jobs.db itself is left in the grant, unused, as
        # a rollback path during the cutover -- drop once jobs-read.db has
        # been live and verified for a while.
        Sid    = "PartitionsAndKnownReadWrite"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.data.arn}/jobs.db",
          "${aws_s3_bucket.data.arn}/jobs-partition-*",
          # descriptions/*: written by load_to_sqlite.py when a job's
          # description is new or changed (loader/descriptions.py).
          "${aws_s3_bucket.data.arn}/descriptions/*",
          # deltas/*: one fragment per sweep, holding only the companies
          # that changed. Replayed into jobs-read.db by the applier.
          "${aws_s3_bucket.data.arn}/deltas/*",
          "${aws_s3_bucket.data.arn}/known.json",
          # status.json: this Lambda's own real-time phase, written at
          # each stage (scraping/loading/sending alerts/idle/error) --
          # see scrape_handler.py's _write_status. Best-effort, never
          # allowed to fail the actual run, but still needs write access.
          "${aws_s3_bucket.data.arn}/status.json",
          # descriptions/*: written by load_to_sqlite.py when a job's
          # description is new or changed (loader/descriptions.py).
          "${aws_s3_bucket.data.arn}/descriptions/*",
        ]
      },
      {
        # Conditional-poll validators, read before the probe step and
        # written back after it. BatchGetItem/BatchWriteItem because a
        # shard is up to SHARD_SIZE companies and one round trip beats
        # fifty; the singular forms are the fallback path for a partial
        # batch response, which DynamoDB is allowed to return.
        Sid    = "ReadWriteScrapeState"
        Effect = "Allow"
        Action = [
          "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem",
          "dynamodb:GetItem", "dynamodb:PutItem",
        ]
        Resource = aws_dynamodb_table.scrape_state.arn
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
  memory_size      = var.scrape_fast_memory_mb
  timeout          = var.scrape_fast_timeout_s

  # Was 1024 the same day: true that VACUUM's own ~2x-DB-size scratch
  # need doesn't apply anymore (see load_to_sqlite.py's --skip-vacuum),
  # but jobs.db still has to be downloaded in full regardless of shard
  # size, and it passed 795MB a few hours later -- barely 230MB of
  # margin left for everything else in /tmp. Bumped to 3008MB, same
  # number and same reasoning as scrape_workday's own same-day fix.
  ephemeral_storage {
    size = 3008
  }

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
      DATA_BUCKET        = aws_s3_bucket.data.bucket
      ALERTS_TABLE       = aws_dynamodb_table.alerts.name
      SCRAPE_STATE_TABLE = aws_dynamodb_table.scrape_state.name
      # How many runs cover every company. 1 is a true global sweep and
      # the destination, but the write side is not ready for it: a sweep
      # that touches N companies touches every shard those companies
      # live in, and one partition write is a 48MB pull-modify-push
      # taking 50-170s. At 4 windows a run wanted 18 partitions and got
      # through 1 before the function ran out, discarding the rest of
      # the work. 24 keeps a run to roughly 145 companies and ~3
      # partitions, which fits, and still covers everything every ~2
      # hours against the old 5.8. Lowering this further needs the delta
      # write path, not a bigger timeout.
      # 1: the full global sweep, every company on every tick. This was
      # 24 because persisting a sweep meant rewriting every partition it
      # touched, 48MB and 50-170s each, so a wide sweep wrote 1 of 18 and
      # binned the rest. The sweep now writes one small delta fragment
      # instead, so that ceiling is gone.
      SWEEP_WINDOWS     = "1"
      ALERTS_FROM_EMAIL = var.alerts_from_email
      SITE_ORIGIN       = "https://${var.domain_name}"
      # Must match schedule_expression below in real seconds. Confirmed
      # live (2026-09-08): during the interim 20-minute cut, this was
      # left hardcoded at 300 in scrape_handler.py while the actual
      # EventBridge rate was 1200s -- current_shard_index() then advanced
      # 4 shards per real invocation instead of 1, and depending on
      # gcd(4, NUM_SHARDS), some shards could go unpolled indefinitely
      # rather than just less often. Passed in from here now so the two
      # can't drift apart silently again the next time this schedule
      # changes.
      SCHEDULE_INTERVAL_S = "300"
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
  description = "Fires the fast re-poll Lambda -- each invocation only handles one shard (see scrape_handler.py), not a full re-poll."
  # Was 5 minutes, cut to 20 the same day (2026-09-08) as an interim
  # tradeoff: sharding decoupled the PROBE cost from company count, but
  # not load_to_sqlite.py's own pull-modify-push cycle, which downloaded
  # and uploaded the FULL jobs.db on every invocation regardless of shard
  # size, and this project has a hard <$5/month ceiling with no room to
  # exceed it even temporarily. Restored to 5 minutes the same day, once
  # Partition & Merge shipped: this Lambda now writes its own small
  # jobs-partition-{N}.db (see scrape_handler.py, load_to_sqlite.py's
  # --key/--skip-known), so cost no longer scales with jobs.db's total
  # size at all -- back to 5 minutes is projected CHEAPER than the 20-
  # minute interim cut was (partition-merge.html §03: 156,000 vs 286,000
  # GB-s/mo), not a tradeoff this time.
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
