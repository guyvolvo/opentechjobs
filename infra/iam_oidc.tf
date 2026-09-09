# GitHub Actions authenticates via OIDC, not long-lived access keys. Each
# workflow assumes a role scoped to exactly what it needs:
#   - infra-deploy: broad (Terraform owns S3/Lambda/CloudFront/IAM), but
#     only assumable from pushes to the deploy branch.
#   - data-deploy: narrow, jobs.db + known.json only, for the scrape
#     workflows. Can't touch infra.
#   - api-deploy: narrow, update the Lambda's code, nothing else.
#   - frontend-deploy: narrow, sync frontend/, invalidate this one
#     distribution. Can't touch jobs.db, the Lambda, or infra.

# GitHub's OIDC provider is an account-wide singleton. AWS allows only
# one per URL per account, and this account's already has one (created by
# the guyvoloshin.com portfolio's own Terraform). Read it rather than
# trying to own/create it here, so the two projects' state never fight
# over the same resource.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

locals {
  # GitHub's actual sub claim is ID-suffixed ("owner@id/repo@id"), not the
  # plain "org/name" form -- see the variables' own comments for why.
  github_owner_name = split("/", var.github_repo)[0]
  github_repo_name  = split("/", var.github_repo)[1]
  github_oidc_sub   = "repo:${local.github_owner_name}@${var.github_owner_id}/${local.github_repo_name}@${var.github_repo_id}:ref:refs/heads/${var.github_deploy_branch}"
}

# infra-deploy: used by deploy-infra.yml (terraform plan/apply)

resource "aws_iam_role" "infra_deploy" {
  name = "${var.project_name}-infra-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = local.github_oidc_sub }
      }
    }]
  })
}

# Scoped to the resource types/prefixes this project's Terraform actually
# manages, not AdministratorAccess, hence branch-restricted above and
# separate from the much narrower data/api-deploy roles below.
resource "aws_iam_role_policy" "infra_deploy" {
  name = "${var.project_name}-infra-deploy-policy"
  role = aws_iam_role.infra_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TerraformState"
        Effect = "Allow"
        # DeleteObject is for releasing the native S3 lock file
        # (use_lockfile in versions.tf), not for the state object itself.
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
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
        # DescribeLogGroups is a list operation, not scopable to one log
        # group's ARN the way ManageLambdaLogs above is.
        Sid      = "ListLogGroups"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
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
        Action = ["iam:GetOpenIDConnectProvider", "iam:ListOpenIDConnectProviders",
          "iam:CreateOpenIDConnectProvider",
        "iam:UpdateOpenIDConnectProviderThumbprint", "iam:TagOpenIDConnectProvider"]
        Resource = "*"
      },
      {
        Sid      = "ManageCloudFront"
        Effect   = "Allow"
        Action   = ["cloudfront:*"]
        Resource = "*" # CloudFront ARNs aren't known until distribution creation; scope via Sid intent, not Resource
      },
      {
        Sid      = "ManageApiGateway"
        Effect   = "Allow"
        Action   = ["apigateway:*"]
        Resource = "*" # API Gateway v2 doesn't support resource-level scoping for most actions
      },
      {
        Sid      = "ManageAcmCertificate"
        Effect   = "Allow"
        Action   = ["acm:*"]
        Resource = "arn:aws:acm:us-east-1:*:certificate/*" # certs are always us-east-1, see acm.tf
      },
      {
        Sid      = "ManageEventBridge"
        Effect   = "Allow"
        Action   = ["events:*"]
        Resource = "arn:aws:events:${var.aws_region}:*:rule/${var.project_name}-*"
      },
      {
        Sid      = "ManageAlertsTable"
        Effect   = "Allow"
        Action   = ["dynamodb:*"]
        Resource = ["arn:aws:dynamodb:${var.aws_region}:*:table/${var.project_name}-*", "arn:aws:dynamodb:${var.aws_region}:*:table/${var.project_name}-*/index/*"]
      },
      {
        # User pool ID is auto-generated at create time, so the ARN can't
        # be scoped down to one specific pool the way the Lambda/DynamoDB
        # statements above are -- region + account is the narrowest this
        # can get before the pool exists.
        Sid      = "ManageCognito"
        Effect   = "Allow"
        Action   = ["cognito-idp:*"]
        Resource = "arn:aws:cognito-idp:${var.aws_region}:*:userpool/*"
      },
      {
        # Domain operations (Describe/Create/UpdateUserPoolDomain) aren't
        # ARN-scopable at all -- confirmed live, AccessDeniedException
        # names the resource as literally "*", not a rejected ARN guess.
        Sid      = "ManageCognitoDomain"
        Effect   = "Allow"
        Action   = ["cognito-idp:DescribeUserPoolDomain", "cognito-idp:CreateUserPoolDomain", "cognito-idp:UpdateUserPoolDomain", "cognito-idp:DeleteUserPoolDomain"]
        Resource = "*"
      },
      {
        Sid      = "ManageSes"
        Effect   = "Allow"
        Action   = ["ses:*", "sesv2:*"]
        Resource = "*" # identity/DKIM/MAIL FROM verification state isn't consistently ARN-scopable across the v1/v2 API split this project's resources straddle
      }
    ]
  })
}

# data-deploy: used by both scrape workflows. jobs.db/known.json only, nothing else.

resource "aws_iam_role" "data_deploy" {
  name = "${var.project_name}-data-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
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
    Statement = [
      {
        Sid    = "JobsDbAndKnownReadWrite"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        # jobs-full-discover.db: both scrape-discover.yml and
        # scrape-fast.yml's manual-dispatch path pull-then-push it --
        # renamed from jobs.db (Partition & Merge, 2026-09-08): a
        # dedicated, never-merged working snapshot for these two full-set
        # workflows, since neither fits the numbered-shard or pinned-
        # partition shape the Lambdas use -- see scrape-discover.yml's own
        # comment on that rename. jobs.db is left in the grant, unused, as
        # a rollback path during the cutover. known.json: scrape-fast.yml
        # downloads it before probing; loader.py re-derives and re-pushes
        # it after every load. status.json: scrape-discover.yml's own
        # real-time phase during its own run -- see the workflow's own
        # status-writing steps.
        Resource = [
          "${aws_s3_bucket.data.arn}/jobs.db",
          "${aws_s3_bucket.data.arn}/jobs-full-discover.db",
          "${aws_s3_bucket.data.arn}/known.json",
          "${aws_s3_bucket.data.arn}/status.json",
          # company-names.json: resolve-company-names.yml reads known.json
          # to find companies without a name, asks each one's own ATS, and
          # writes the result here. The merge picks it up on its next run
          # and stamps the names onto jobs-read.db.
          "${aws_s3_bucket.data.arn}/company-names.json",
          # jobs-read.db: build-salary-matrix.yml reads the live snapshot
          # to find every listing that discloses a real range. Read is
          # what it needs; PutObject on this key is granted only because
          # this statement covers both actions for every key it lists, and
          # nothing in that workflow writes the snapshot. Worth watching:
          # this is the one grant here that touches the file the API
          # serves from, and a bug that wrote it would be a live outage
          # rather than a stale side file.
          "${aws_s3_bucket.data.arn}/jobs-read.db",
          # salary-matrix.json: the cells of disclosed pay the estimator
          # looks up (build_salary_matrix.py). Same shape as
          # company-names.json above, a small side file the pipeline reads
          # rather than anything on the serving path.
          "${aws_s3_bucket.data.arn}/salary-matrix.json",
        ]
      },
      {
        # merge-discovered-companies.yml's own safety check: read-only,
        # scoped to just the fast-poll Lambda's own log group, before
        # merging each overnight batch -- confirms its most recent real
        # Duration still has margin before adding more companies to what
        # it checks every 5 minutes, rather than finding out live that a
        # batch pushed it over budget.
        Sid      = "ReadScrapeFastLogs"
        Effect   = "Allow"
        Action   = ["logs:FilterLogEvents", "logs:DescribeLogStreams"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-scrape-fast*"
      }
    ]
  })
}

# api-deploy: used by deploy-api.yml. Update Lambda code, nothing else.

resource "aws_iam_role" "api_deploy" {
  name = "${var.project_name}-api-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
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
      Sid    = "UpdateApiLambdaCode"
      Effect = "Allow"
      # GetFunctionConfiguration is what `aws lambda wait function-updated`
      # actually polls, distinct from GetFunction.
      Action   = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:PublishVersion"]
      Resource = aws_lambda_function.api.arn
    }]
  })
}

# scrape-lambda-deploy: used by deploy-scrape-lambda.yml. Update the
# scrape-fast, scrape-workday, and scrape-maintenance Lambdas' code,
# nothing else.

resource "aws_iam_role" "scrape_lambda_deploy" {
  name = "${var.project_name}-scrape-lambda-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = local.github_oidc_sub }
      }
    }]
  })
}

resource "aws_iam_role_policy" "scrape_lambda_deploy" {
  name = "${var.project_name}-scrape-lambda-deploy-policy"
  role = aws_iam_role.scrape_lambda_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "UpdateScrapeLambdaCode"
      Effect   = "Allow"
      Action   = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:PublishVersion"]
      Resource = [aws_lambda_function.scrape_fast.arn, aws_lambda_function.scrape_workday.arn, aws_lambda_function.scrape_maintenance.arn]
    }]
  })
}

# github-auth-lambda-deploy: used by deploy-github-auth-lambda.yml.
# Update the github-auth Lambda's code, nothing else.

resource "aws_iam_role" "github_auth_lambda_deploy" {
  name = "${var.project_name}-github-auth-lambda-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = local.github_oidc_sub }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_auth_lambda_deploy" {
  name = "${var.project_name}-github-auth-lambda-deploy-policy"
  role = aws_iam_role.github_auth_lambda_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "UpdateGithubAuthLambdaCode"
      Effect   = "Allow"
      Action   = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:PublishVersion"]
      Resource = aws_lambda_function.github_auth.arn
    }]
  })
}

# frontend-deploy: used by deploy-frontend.yml. Sync frontend/ to the
# frontend bucket, invalidate this one distribution. Nothing else.

resource "aws_iam_role" "frontend_deploy" {
  name = "${var.project_name}-frontend-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
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
        # `aws s3 sync --delete` needs List (to diff) and Delete (to
        # remove dropped files), not just Put.
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
