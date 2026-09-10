# Data bucket: jobs.db (read only by the Lambda's IAM role), raw
# resolved.json, and Parquet snapshots. Private, nothing here is served
# directly to the public.

resource "aws_s3_bucket" "data" {
  bucket = var.data_bucket_name
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled" # jobs.db version history = a free rollback if a bad load ships
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  # jobs-read.db is the exception, and it needs its own rule.
  #
  # The 30-day rule below was written when an hourly merge rewrote this
  # file 24 times a day. Delta fragments moved that to every 5 minutes,
  # and versioning turns every one of those rewrites into a retained
  # ~790MB object. Measured 2026-09-10, two days after the migration:
  # 275 versions holding 198GB, on track for roughly 4TB and $100 a
  # month once the 30-day window actually fills.
  #
  # None of that is a usable rollback target. A snapshot from last week
  # is a snapshot missing a week of listings, so restoring it would be a
  # worse outage than whatever it was meant to undo. One day is already
  # 180 versions to choose from.
  rule {
    id     = "expire-snapshot-versions"
    status = "Enabled"
    filter {
      prefix = "jobs-read.db"
    }
    noncurrent_version_expiration {
      # Both conditions have to hold before a version goes, so the 3 is
      # a floor for the case where writes have stopped: a stuck pipeline
      # must not quietly age out the last good snapshot.
      newer_noncurrent_versions = 3
      noncurrent_days           = 1
    }
  }

  rule {
    id     = "expire-old-jobsdb-versions"
    status = "Enabled"
    filter {} # everything else here is small and rarely rewritten
    noncurrent_version_expiration {
      noncurrent_days = 30 # keep a month of rollback history, not forever
    }
  }

  # A 790MB upload_file goes out as multipart. A failed one leaves its
  # parts behind, billed as storage, invisible to a plain ListObjects.
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }
}

# Frontend bucket: static site. Private, served only via CloudFront's
# Origin Access Control; nobody hits S3 directly.

resource "aws_s3_bucket" "frontend" {
  bucket = var.frontend_bucket_name
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontOAC"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
      Condition = {
        StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.main.arn }
      }
    }]
  })
}
