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
    allow_methods = ["GET"]
    allow_headers = ["content-type"]
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
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}
