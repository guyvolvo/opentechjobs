# Single CloudFront distribution, single domain: default behavior serves
# the static frontend from S3 (via Origin Access Control -- the bucket
# itself stays private), /api/* routes to the Lambda Function URL. One
# domain for both means the frontend never needs CORS for its own API
# calls; the Function URL's CORS config in lambda.tf only matters for
# anyone hitting the API directly (which the brief wants supported --
# "anyone can poll it").

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.project_name}-frontend-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# CloudFront needs a bare domain for a custom origin, not the full URL --
# Lambda Function URLs are always "https://<id>.lambda-url.<region>.on.aws/".
locals {
  lambda_url_domain = replace(replace(aws_lambda_function_url.api.function_url, "https://", ""), "/", "")
}

resource "aws_cloudfront_cache_policy" "api" {
  name        = "${var.project_name}-api-cache"
  comment     = "Short TTL: known companies get re-polled every 10 min (scrape-fast.yml), so the edge cache should track that, not sit stale for hours -- long enough to absorb repeat traffic between polls, short enough that 'poll the API' stays a meaningful way to get fresh data per the brief's no-alerting-tier design."
  default_ttl = 120
  min_ttl     = 0
  max_ttl     = 3600
  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config {
      query_string_behavior = "all" # /jobs?ats=greenhouse&q=... etc. must vary the cached response
    }
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true
  }
}

resource "aws_cloudfront_distribution" "main" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100" # NA+EU edge locations only -- cheapest tier; fine for an IL-focused audience mostly browsing from IL/EU/US

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "frontend-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  origin {
    domain_name = local.lambda_url_domain
    origin_id   = "api-lambda"
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "frontend-s3"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = data.aws_cloudfront_cache_policy.caching_optimized.id
    compress               = true
  }

  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "api-lambda"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = aws_cloudfront_cache_policy.api.id
    compress               = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # swap for an ACM cert + aliases once a custom domain is wired up
  }
}

data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}
