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

# Short TTL: known companies get re-polled every 5 min (the EventBridge-
# scheduled scrape-fast Lambda, see scrape_handler.py -- not
# scrape-fast.yml itself, which is workflow_dispatch-only), so the edge
# cache should track that rather than serve outdated data for hours.
# (CloudFront's Comment field caps at 128 chars, hence the terse version
# there and the full one here.)
resource "aws_cloudfront_cache_policy" "api" {
  name        = "${var.project_name}-api-cache"
  comment     = "Short TTL tracking the 5-min scrape cadence"
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

  # More specific than /api/* below, so this one wins for exactly this
  # path -- ordered_cache_behavior blocks are evaluated in the order
  # Terraform lists them, same as CloudFront's own path-pattern
  # precedence. /api/*'s own cache policy (header_behavior = "none")
  # turned out to strip the Authorization header before it ever reached
  # origin, not just from the cache key -- confirmed live: GET
  # /me/alerts came back "Unauthorized" with a token that had just
  # authenticated a POST to the same path seconds earlier, because POST
  # (never cached) passes every header through while GET (cacheable)
  # doesn't. Worse than a functional bug: a shared cache key that
  # ignores Authorization on a per-user route risks serving one user's
  # cached response to a different, differently-authenticated request
  # for the same URL.
  #
  # Two policies, not one: a cache policy's header/cookie allowlist only
  # ever describes the cache key, and CloudFront's API flatly rejects a
  # non-"none" header_behavior on a caching-disabled (TTL all zero)
  # policy as meaningless -- confirmed live. What forwards to origin is
  # the separate Origin Request Policy concept; CachingDisabled +
  # AllViewer is AWS's own recommended pairing for exactly this shape
  # (a dynamic, authenticated path that must never be cached).
  ordered_cache_behavior {
    path_pattern             = "/api/me/*"
    target_origin_id         = "api-lambda"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = true
  }

  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "api-lambda"
    viewer_protocol_policy = "redirect-to-https"
    # CloudFront only accepts one of three fixed sets here, not an
    # arbitrary subset -- GET/HEAD, GET/HEAD/OPTIONS, or all seven. The
    # full set is needed now that /api/* carries real write routes
    # (email/start POST; the alerts CRUD routes to come are PUT/PATCH/
    # DELETE) -- confirmed live: a plain POST 403'd ("only cachable
    # requests") against the GET/HEAD/OPTIONS-only set this used to be.
    allowed_methods = ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]
    cached_methods  = ["GET", "HEAD"] # never cache a write response, regardless of what's allowed through
    cache_policy_id = aws_cloudfront_cache_policy.api.id
    compress        = true
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

# A cache policy's own header/cookie allowlist only ever describes the
# cache key -- confirmed live, CloudFront's API flatly rejects a
# non-"none" header_behavior on a policy with caching disabled
# (TTL all zero) as a meaningless combination, which it is: nothing
# gets cached, so there's no cache key for a header to vary. What
# forwards to origin is a genuinely separate concept, Origin Request
# Policy, needed alongside this one -- see origin_request_policy below.
data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

# Except-Host, not plain AllViewer: this API Gateway has no custom
# domain name mapped (it's addressed via its own raw execute-api
# domain, see local.api_gateway_domain), so it only recognizes Host
# headers matching that domain. Plain AllViewer forwards the *viewer's*
# original Host (openmarket.guyvoloshin.com) instead of the origin's --
# API Gateway doesn't know that domain, and likely rejected the request
# on that mismatch before authorization even ran, not a genuine JWT
# validation failure. This is AWS's own documented fix for exactly this
# "API Gateway (or any origin without a matching custom domain) behind
# CloudFront" shape: forward everything else, let CloudFront's own
# custom_origin_config supply the correct Host.
data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewerExceptHostHeader"
}
