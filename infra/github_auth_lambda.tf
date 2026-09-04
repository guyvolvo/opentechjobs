# GitHub sign-in's own Lambda -- both the OAuth callback HTTP route and
# Cognito's three custom-auth triggers, one function dispatched by event
# shape. See github_auth_handler.py's own docstring for the full flow and
# why this can't just be a Cognito identity provider like Google.
#
# Deliberately its own Lambda + role, not folded into the read-only api
# Lambda: keeps AdminCreateUser/AdminInitiateAuth-class IAM permissions
# out of the Lambda that serves fully public, unauthenticated job data.

data "archive_file" "github_auth" {
  type        = "zip"
  source_file = "${path.module}/../github_auth_handler.py"
  output_path = "${path.module}/build/github-auth.zip"
}

resource "aws_iam_role" "github_auth_lambda" {
  name = "${var.project_name}-github-auth-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "github_auth_lambda" {
  name = "${var.project_name}-github-auth-lambda-policy"
  role = aws_iam_role.github_auth_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DriveCustomAuthForGithubUsers"
        Effect = "Allow"
        Action = [
          "cognito-idp:AdminGetUser",
          "cognito-idp:AdminCreateUser",
          "cognito-idp:AdminSetUserPassword",
          "cognito-idp:AdminInitiateAuth",
          "cognito-idp:AdminRespondToAuthChallenge",
        ]
        Resource = aws_cognito_user_pool.main.arn
      },
      {
        # List operations, not scopable to one pool's ARN -- how the
        # handler finds its own pool/client ID without a Terraform
        # dependency cycle (see github_auth_handler.py's own comment).
        Sid      = "FindOwnPoolAndClient"
        Effect   = "Allow"
        Action   = ["cognito-idp:ListUserPools", "cognito-idp:ListUserPoolClients"]
        Resource = "*"
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-github-auth*"
      }
    ]
  })
}

resource "aws_lambda_function" "github_auth" {
  function_name    = "${var.project_name}-github-auth"
  role             = aws_iam_role.github_auth_lambda.arn
  handler          = "github_auth_handler.lambda_handler"
  runtime          = "python3.13"
  filename         = data.archive_file.github_auth.output_path
  source_code_hash = data.archive_file.github_auth.output_base64sha256
  memory_size      = 128
  timeout          = 15 # three sequential outbound HTTPS calls (GitHub token, GitHub user, GitHub emails) plus two Cognito admin calls

  environment {
    variables = {
      GITHUB_CLIENT_ID     = var.github_oauth_client_id
      GITHUB_CLIENT_SECRET = var.github_oauth_client_secret
      POOL_NAME            = "${var.project_name}-users" # matches aws_cognito_user_pool.main's name; see github_auth_handler.py's lookup comment
      SITE_ORIGIN          = "https://${var.domain_name}"
    }
  }

  # Same reasoning as the other two Lambdas' lifecycle blocks (see
  # scrape_lambda.tf) -- this file's own archive_file has no way to
  # bundle dependencies, so its zip is a placeholder past initial
  # create; deploy-github-auth-lambda.yml owns the real code.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

resource "aws_cloudwatch_log_group" "github_auth_lambda" {
  name              = "/aws/lambda/${aws_lambda_function.github_auth.function_name}"
  retention_in_days = 14
}

resource "aws_lambda_permission" "cognito_invoke_github_auth" {
  statement_id  = "AllowCognitoInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.github_auth.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.main.arn
}

resource "aws_lambda_permission" "apigw_invoke_github_auth" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.github_auth.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

resource "aws_apigatewayv2_integration" "github_auth" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.github_auth.invoke_arn
  payload_format_version = "2.0"
}

# Both more specific than aws_apigatewayv2_route.api's "ANY /{proxy+}"
# catch-all in apigateway.tf, so these two win for exactly these paths;
# everything else keeps routing to the main api Lambda unchanged.
resource "aws_apigatewayv2_route" "github_auth_callback" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "GET /api/auth/github/callback"
  target    = "integrations/${aws_apigatewayv2_integration.github_auth.id}"
}

resource "aws_apigatewayv2_route" "email_auth_start" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /api/auth/email/start"
  target    = "integrations/${aws_apigatewayv2_integration.github_auth.id}"
}
