# ---------------------------------------------------------------------------
# Data bucket: jobs.db (read only by the Lambda's IAM role), raw
# resolved.json, and Parquet snapshots. Private, nothing here is served
# directly to the public.
# ---------------------------------------------------------------------------

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
  rule {
    id     = "expire-old-jobsdb-versions"
    status = "Enabled"
    filter {} # applies to every object in the bucket; there's only jobs.db in here
    noncurrent_version_expiration {
      noncurrent_days = 30 # keep a month of rollback history, not forever
    }
  }
}

# ---------------------------------------------------------------------------
# Frontend bucket: static site. Private, served only via CloudFront's
# Origin Access Control; nobody hits S3 directly.
# ---------------------------------------------------------------------------

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
