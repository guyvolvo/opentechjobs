# Single CloudFront distribution, single domain: default behavior serves
# the static frontend from S3 (via Origin Access Control, bucket stays
# private), /api/* routes to the API Gateway origin (see apigateway.tf).
# One domain for both means the frontend never needs CORS for its own
# calls; the API's own CORS config only matters for direct API access.

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.project_name}-frontend-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# CloudFront needs a bare domain for a custom origin. HTTP API domains
# follow a fixed shape, no need to parse a URL like the Function URL did.
locals {
  api_gateway_domain = "${aws_apigatewayv2_api.api.id}.execute-api.${var.aws_region}.amazonaws.com"
}

# Short TTL: known companies get re-polled every 10 min (scrape-fast.yml),
# so the edge cache should track that rather than sit stale for hours.
# (CloudFront's Comment field caps at 128 chars, hence the terse version
# there and the full one here.)
resource "aws_cloudfront_cache_policy" "api" {
  name        = "${var.project_name}-api-cache"
  comment     = "Short TTL tracking the 10-min scrape cadence"
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
  price_class         = "PriceClass_100" # NA+EU edge locations only, cheapest tier; fine for an IL-focused audience mostly browsing from IL/EU/US
  aliases             = [var.domain_name]

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "frontend-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  origin {
    domain_name = local.api_gateway_domain
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
    acm_certificate_arn      = aws_acm_certificate_validation.site.certificate_arn
    ssl_support_method       = "sni-only" # free; the alternative (vip) costs ~$600/mo and isn't needed for a modern SNI-capable domain
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}
