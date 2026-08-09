# GitHub Actions authenticates to AWS via OIDC, not long-lived access keys
# -- no AWS secret sits in GitHub's secret store waiting to leak. Each
# workflow assumes a role scoped to exactly what it needs:
#   - infra-deploy: broad (Terraform manages S3/Lambda/CloudFront/IAM
#     itself), but only assumable from pushes to the deploy branch.
#   - data-deploy: narrow -- just jobs.db + known.json read/write on the
#     data bucket. This is what scrape-fast.yml (every 10 min) and
#     scrape-discover.yml (daily) both use; it should never be able to
#     touch infra.
#   - api-deploy: narrow -- just update the Lambda's code. Nothing else.
#   - frontend-deploy: narrow -- sync frontend/ to the frontend bucket and
#     invalidate this one CloudFront distribution. Can't touch jobs.db,
#     the Lambda, or anything data-deploy/api-deploy can touch.

data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]
}

locals {
  github_oidc_sub = "repo:${var.github_repo}:ref:refs/heads/${var.github_deploy_branch}"
}

# ---------------------------------------------------------------------------
# infra-deploy: used by deploy-infra.yml (terraform plan/apply)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "infra_deploy" {
  name = "${var.project_name}-infra-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = local.github_oidc_sub }
      }
    }]
  })
}

# Scoped to the resource types/prefixes this project's Terraform actually
# manages -- not AdministratorAccess. Still broad within those bounds
# (Terraform needs to create/modify/delete IAM roles, since it owns the
# Lambda execution role), which is why this role is branch-restricted above
# and separate from the much narrower data/api-deploy roles below.
resource "aws_iam_role_policy" "infra_deploy" {
  name = "${var.project_name}-infra-deploy-policy"
  role = aws_iam_role.infra_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TerraformState"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.project_name}-tfstate-*",
          "arn:aws:s3:::${var.project_name}-tfstate-*/*",
        ]
      },
      {
        Sid    = "ManageProjectBuckets"
        Effect = "Allow"
        Action = "s3:*"
        Resource = [
          aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*",
          aws_s3_bucket.frontend.arn, "${aws_s3_bucket.frontend.arn}/*",
        ]
      },
      {
        Sid      = "ManageLambda"
        Effect   = "Allow"
        Action   = ["lambda:*"]
        Resource = "arn:aws:lambda:${var.aws_region}:*:function:${var.project_name}-*"
      },
      {
        Sid      = "ManageLambdaLogs"
        Effect   = "Allow"
        Action   = ["logs:*"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-*"
      },
      {
        Sid    = "ManageIamForLambda"
        Effect = "Allow"
        Action = ["iam:GetRole", "iam:CreateRole", "iam:DeleteRole", "iam:PutRolePolicy",
          "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:PassRole", "iam:TagRole",
        "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"]
        Resource = "arn:aws:iam::*:role/${var.project_name}-*"
      },
      {
        Sid    = "ManageOidcProvider"
        Effect = "Allow"
        Action = ["iam:GetOpenIDConnectProvider", "iam:CreateOpenIDConnectProvider",
        "iam:UpdateOpenIDConnectProviderThumbprint", "iam:TagOpenIDConnectProvider"]
        Resource = "*"
      },
      {
        Sid      = "ManageCloudFront"
        Effect   = "Allow"
        Action   = ["cloudfront:*"]
        Resource = "*" # CloudFront ARNs aren't known until distribution creation; scope via Sid intent, not Resource
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# data-deploy: used by both scrape workflows -- jobs.db/known.json only, nothing else.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "data_deploy" {
  name = "${var.project_name}-data-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = local.github_oidc_sub }
      }
    }]
  })
}

resource "aws_iam_role_policy" "data_deploy" {
  name = "${var.project_name}-data-deploy-policy"
  role = aws_iam_role.data_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "JobsDbAndKnownReadWrite"
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:PutObject"]
      # jobs.db: both scrape-discover.yml and scrape-fast.yml pull-then-push it.
      # known.json: scrape-fast.yml downloads it directly (aws s3 cp) before
      # probing; loader.py re-derives and re-pushes it after every load, either
      # workflow -- see export_known()'s docstring for why it's every load.
      Resource = [
        "${aws_s3_bucket.data.arn}/jobs.db",
        "${aws_s3_bucket.data.arn}/known.json",
      ]
    }]
  })
}

# ---------------------------------------------------------------------------
# api-deploy: used by deploy-api.yml -- update Lambda code, nothing else.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "api_deploy" {
  name = "${var.project_name}-api-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = local.github_oidc_sub }
      }
    }]
  })
}

resource "aws_iam_role_policy" "api_deploy" {
  name = "${var.project_name}-api-deploy-policy"
  role = aws_iam_role.api_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "UpdateApiLambdaCode"
      Effect   = "Allow"
      Action   = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:PublishVersion"]
      Resource = aws_lambda_function.api.arn
    }]
  })
}

# ---------------------------------------------------------------------------
# frontend-deploy: used by deploy-frontend.yml -- sync frontend/ to the
# frontend bucket, invalidate this one distribution. Nothing else.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "frontend_deploy" {
  name = "${var.project_name}-frontend-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = local.github_oidc_sub }
      }
    }]
  })
}

resource "aws_iam_role_policy" "frontend_deploy" {
  name = "${var.project_name}-frontend-deploy-policy"
  role = aws_iam_role.frontend_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SyncFrontendBucket"
        Effect = "Allow"
        # `aws s3 sync --delete` needs List to diff against what's already
        # there and Delete to remove files dropped from frontend/, not
        # just Put -- narrower than that would make --delete silently
        # leave stale files behind instead of failing loudly.
        Action = ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = [
          aws_s3_bucket.frontend.arn,
          "${aws_s3_bucket.frontend.arn}/*",
        ]
      },
      {
        Sid      = "InvalidateThisDistributionOnly"
        Effect   = "Allow"
        Action   = ["cloudfront:CreateInvalidation"]
        Resource = aws_cloudfront_distribution.main.arn
      }
    ]
  })
}
