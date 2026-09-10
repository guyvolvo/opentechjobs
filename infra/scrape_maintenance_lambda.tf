# Was once-daily VACUUM-only, split out of both frequent re-poll cycles
# on 2026-09-08 to get this project's whole AWS bill under a hard
# <$5/month ceiling. Same day, later: this became the Partition & Merge
# design's own merge step (loader/merge_partitions.py) -- see
# scrape_maintenance_handler.py's own docstring for the full picture.
# Runs hourly now, not daily: jobs-read.db (api/db.py's DATA_KEY) only
# gets fresher when this runs, so its cadence is now the real ceiling on
# how stale the live site's listings can be, not a free-standing
# maintenance detail. Mirrors scrape_lambda.tf's own resource shapes;
# kept in its own file for the same reason scrape_workday_lambda.tf is:
# this function's reason for existing is genuinely distinct from the
# rest of the fleet's.

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
        # merge_partitions.py's own list_partitions() -- s3:ListBucket is
        # a bucket-level permission, not an object one, so it's separate
        # from the GetObject/PutObject grant below. No other Lambda in
        # this project has ever needed to list objects before this;
        # scoped by prefix so it can't enumerate the rest of the bucket
        # (frontend assets live in a different bucket entirely, but
        # status.json/companies data share this one).
        Sid      = "ListPartitions"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.data.arn
        Condition = {
          StringLike = { "s3:prefix" = ["jobs-partition-*", "deltas/*"] }
        }
      },
      {
        # known.json: read-only here -- see merge_partitions.py's own
        # docstring for why the merge step must never write it (a
        # partition's-eye view would be incomplete; known.json stays the
        # full discovery batch's job alone). jobs-read.db: the merged
        # snapshot this Lambda produces, api/db.py's DATA_KEY.
        # merge-status.json: this Lambda's own real-time phase -- see
        # scrape_maintenance_handler.py's own _write_status.
        Sid    = "PartitionsReadKnownReadWriteSnapshot"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = [
          "${aws_s3_bucket.data.arn}/jobs-partition-*",
          "${aws_s3_bucket.data.arn}/known.json",
          "${aws_s3_bucket.data.arn}/jobs-read.db",
          "${aws_s3_bucket.data.arn}/merge-status.json",
          # company-names.json: read-only here. Written by
          # resolve-company-names.yml; this Lambda only applies it to the
          # snapshot it just built (see apply_company_names).
          "${aws_s3_bucket.data.arn}/company-names.json",
          # deltas/*: read and then deleted once a snapshot containing
          # them has been pushed. See loader/deltas.py.
          "${aws_s3_bucket.data.arn}/deltas/*",
          # descriptions/*: the applier now runs the loader, so it writes
          # description blobs too.
          "${aws_s3_bucket.data.arn}/descriptions/*",
        ]
      },
      {
        # bootstrap.json only. The merge is the one moment this data
        # changes and the one process holding the freshly-built snapshot
        # on local disk, so it publishes the site's default first page
        # here as a static object CloudFront can serve from the edge with
        # no Lambda in the request path at all. Scoped to that single
        # key: this role has no other business in the frontend bucket.
        Sid      = "PublishBootstrap"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.frontend.arn}/bootstrap.json"
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
  function_name = "${var.project_name}-scrape-maintenance"
  role          = aws_iam_role.scrape_maintenance_lambda.arn
  handler       = "scrape_maintenance_handler.lambda_handler"
  runtime       = "python3.13"
  # Graviton. Same code, same Python, about 20% less per GB-second, and
  # every package here is either pure Python or installed for this
  # architecture explicitly (see deploy-scrape-lambda.yml). Nothing in
  # this project touches a native x86 dependency.
  architectures    = ["arm64"]
  filename         = data.archive_file.scrape_maintenance.output_path
  source_code_hash = data.archive_file.scrape_maintenance.output_base64sha256
  memory_size      = var.scrape_maintenance_memory_mb
  timeout          = var.scrape_maintenance_timeout_s

  # Confirmed live (2026-09-08): this Lambda's own real first run hit
  # "database or disk is full" (sqlite3.OperationalError) during VACUUM
  # at 3008MB. Unlike Lambda MEMORY (this account's real ceiling is
  # 3008MB, confirmed live elsewhere in this file's history), ephemeral
  # storage isn't subject to that same cap -- up to 10240MB is normally
  # available regardless. Needs all THREE at once, simultaneously, not
  # just one DB's worth: every downloaded jobs-partition-*.db (summing to
  # roughly jobs.db's old total size, ~1.2GB), PLUS the merged jobs-
  # read.db being built (another ~1.2GB), PLUS VACUUM's own scratch copy
  # to rebuild THAT (a third ~1.2GB) -- around 3.6GB total against the
  # 3008MB (2.94GB) this was set to. 8192MB gives real margin above that,
  # not just enough to clear today's number, since partition count and
  # total size only grow from here.
  ephemeral_storage {
    size = 8192
  }

  environment {
    variables = {
      DATA_BUCKET = aws_s3_bucket.data.bucket
      # Unset would simply mean no bootstrap.json gets published and the
      # site keeps fetching its first page from the API, as it did before.
      FRONTEND_BUCKET = aws_s3_bucket.frontend.bucket
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
  name        = "${var.project_name}-scrape-maintenance-schedule"
  description = "Fires the partition merge -- see this file's own header comment for why hourly, not daily, now"
  # 60 minutes: the design doc's own approved target (partition-merge.html
  # §05/§07). Projected at ~190,000 GB-s/mo at this cadence, folded into
  # the overall ~$3.65/mo estimate alongside scrape-fast back at its full
  # 5-minute cadence -- see scrape_lambda.tf's own schedule for that half.
  # 5 minutes, down from 60. The merge was hourly because rebuilding a
  # 1.2GB snapshot took 70 seconds and every API container then had to
  # re-download it inside a user's request. Both of those went away when
  # description text and raw_json left the file: measured live, the
  # snapshot is now 284MB and a full rebuild takes about 10 seconds.
  #
  # This is the second half of getting a new job visible in minutes. The
  # sweep already checks every company every 5 minutes; without this the
  # result still sat in a partition for up to an hour before anyone
  # could see it. Cost is ~85,000 GB-s/month at this cadence, inside the
  # free tier.
  schedule_expression = "rate(5 minutes)"
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
