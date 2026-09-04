# Stand-in for the Lambda Function URL (lambda.tf's original, cheaper
# choice). This AWS account currently can't create Function URLs in
# il-central-1 (AccessDeniedException on create AND list, even with full
# admin access; looks like an account-provisioning gap on AWS's side, not
# a config issue). An HTTP API is the standard fallback: still effectively
# free at this project's traffic, just not literally $0. Revert once the
# account-level block lifts.

resource "aws_apigatewayv2_api" "api" {
  name          = "${var.project_name}-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "PATCH", "DELETE"] # POST is also /api/auth/email/start (github_auth_lambda.tf); PATCH/DELETE are /me/alerts/{id} only
    allow_headers = ["content-type", "authorization"]  # authorization: the Cognito JWT on /me/alerts requests
    max_age       = 3600
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0" # same event shape as a Function URL; handler.py needs no changes
}

# Catch-all: CloudFront forwards the full "/api/*" request URI unchanged
# (no origin_path rewrite), so this needs to match "/api/jobs" etc.
# verbatim, same as the Function URL did.
resource "aws_apigatewayv2_route" "api" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "ANY /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_stage" "api" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true

  # Free built-in cap against a runaway bill from a bot flood or scraper --
  # no WAF, no extra service, just API Gateway's own throttle. Requests
  # past this get a 429 instead of reaching the Lambda. Not per-IP (HTTP
  # API v2 stage throttling is account-wide for this API), but it bounds
  # the worst case regardless of source.
  default_route_settings {
    throttling_rate_limit  = 20
    throttling_burst_limit = 40
  }
}

# Validates the Cognito JWT natively at the API Gateway layer -- an
# unauthenticated or forged-token request never reaches the Lambda for
# the routes below at all, not just rejected in application code.
resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.api.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "${var.project_name}-cognito-jwt"

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.web.id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
  }
}

# More specific than the "ANY /{proxy+}" catch-all above, so these four
# win for exactly these paths; everything else (including GET /me/alerts
# itself if hit with the wrong method) still falls through to the public
# catch-all, which 404s it as "no route" rather than silently allowing
# an unauthenticated method through.
resource "aws_apigatewayv2_integration" "alerts" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "alerts_list_create" {
  for_each  = toset(["GET", "POST"])
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "${each.value} /api/me/alerts"
  target    = "integrations/${aws_apigatewayv2_integration.alerts.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "alerts_update_delete" {
  for_each  = toset(["PATCH", "DELETE"])
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "${each.value} /api/me/alerts/{id}"
  target    = "integrations/${aws_apigatewayv2_integration.alerts.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}
