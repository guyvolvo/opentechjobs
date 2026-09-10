# Replaces two GitHub Actions `schedule:` triggers that don't fire
# reliably (see dispatch_workflow_handler.py's own docstring -- the
# same root cause scrape_handler.py already worked around once for
# scrape-fast.yml, confirmed again 2026-09-08 for
# merge-discovered-companies.yml). EventBridge has a real SLA; GitHub's
# own cron scheduler does not.

data "archive_file" "dispatch_workflow" {
  type        = "zip"
  source_file = "${path.module}/../dispatch_workflow_handler.py"
  output_path = "${path.module}/build/dispatch-workflow.zip"
}

resource "aws_iam_role" "dispatch_workflow_lambda" {
  name = "${var.project_name}-dispatch-workflow-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "dispatch_workflow_lambda" {
  name = "${var.project_name}-dispatch-workflow-lambda-policy"
  role = aws_iam_role.dispatch_workflow_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The dispatch token itself -- a fine-grained PAT scoped to only
        # this one repo's Actions:write, stored out-of-band (not by
        # Terraform, so it's never in state or a plan diff). See
        # dispatch_workflow_handler.py's own docstring.
        Sid      = "ReadDispatchToken"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/iljobs/github-dispatch-token"
      },
      {
        # SSM's own default KMS key for SecureString parameters --
        # granting this Lambda's role decrypt access is what
        # WithDecryption=true in the handler actually needs.
        Sid      = "DecryptDispatchToken"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:aws:kms:${var.aws_region}:876913698688:key/1b214d23-ec87-4851-954a-f44f247b54d0"
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-dispatch-workflow*"
      }
    ]
  })
}

resource "aws_lambda_function" "dispatch_workflow" {
  function_name = "${var.project_name}-dispatch-workflow"
  role          = aws_iam_role.dispatch_workflow_lambda.arn
  handler       = "dispatch_workflow_handler.lambda_handler"
  runtime       = "python3.13"
  # Graviton. Same code, same Python, about 20% less per GB-second, and
  # every package here is either pure Python or installed for this
  # architecture explicitly (see deploy-scrape-lambda.yml). Nothing in
  # this project touches a native x86 dependency.
  architectures    = ["arm64"]
  filename         = data.archive_file.dispatch_workflow.output_path
  source_code_hash = data.archive_file.dispatch_workflow.output_base64sha256
  memory_size      = 128 # a single HTTPS POST, stdlib only -- nothing here needs more
  timeout          = 15
}

resource "aws_cloudwatch_log_group" "dispatch_workflow_lambda" {
  name              = "/aws/lambda/${aws_lambda_function.dispatch_workflow.function_name}"
  retention_in_days = 14
}

# One rule per dispatched workflow, all targeting the same Lambda --
# see that function's own docstring for why the workflow name lives in
# the rule's input, not in separate code.

resource "aws_cloudwatch_event_rule" "dispatch_merge_discovered" {
  name                = "${var.project_name}-dispatch-merge-discovered"
  description         = "Reliably fires merge-discovered-companies.yml every 10 minutes -- its own schedule: trigger doesn't fire on its own (confirmed live 2026-09-08)"
  schedule_expression = "rate(10 minutes)"
}

resource "aws_cloudwatch_event_target" "dispatch_merge_discovered" {
  rule = aws_cloudwatch_event_rule.dispatch_merge_discovered.name
  arn  = aws_lambda_function.dispatch_workflow.arn
  input = jsonencode({
    workflow_file = "merge-discovered-companies.yml"
  })
}

resource "aws_lambda_permission" "allow_eventbridge_dispatch_merge" {
  statement_id  = "AllowEventBridgeInvokeMerge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatch_workflow.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.dispatch_merge_discovered.arn
}

resource "aws_cloudwatch_event_rule" "dispatch_discover_companies" {
  name                = "${var.project_name}-dispatch-discover-companies"
  description         = "Reliably fires discover-companies.yml daily -- same schedule: unreliability as the merge workflow above"
  schedule_expression = "rate(1 day)"
}

resource "aws_cloudwatch_event_target" "dispatch_discover_companies" {
  rule = aws_cloudwatch_event_rule.dispatch_discover_companies.name
  arn  = aws_lambda_function.dispatch_workflow.arn
  input = jsonencode({
    workflow_file = "discover-companies.yml"
  })
}

resource "aws_lambda_permission" "allow_eventbridge_dispatch_discover" {
  statement_id  = "AllowEventBridgeInvokeDiscover"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatch_workflow.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.dispatch_discover_companies.arn
}
