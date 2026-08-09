output "cloudfront_domain" {
  value       = aws_cloudfront_distribution.main.domain_name
  description = "Public URL for the site + API (site at /, API at /api/*)"
}

output "lambda_function_url" {
  value       = aws_lambda_function_url.api.function_url
  description = "Direct Lambda URL, bypassing CloudFront -- useful for debugging cache issues"
}

output "data_bucket" {
  value = aws_s3_bucket.data.bucket
}

output "frontend_bucket" {
  value = aws_s3_bucket.frontend.bucket
}

output "lambda_function_name" {
  value = aws_lambda_function.api.function_name
}

output "cloudfront_distribution_id" {
  value       = aws_cloudfront_distribution.main.id
  description = "For deploy-frontend.yml's CreateInvalidation call -- CLOUDFRONT_DISTRIBUTION_ID repo variable."
}

# Paste these into the repo's Settings -> Actions -> Variables (not
# secrets -- role ARNs aren't sensitive, there's no key material here) so
# the GitHub Actions workflows know which role to assume.
output "github_actions_role_arns" {
  value = {
    infra_deploy    = aws_iam_role.infra_deploy.arn
    data_deploy     = aws_iam_role.data_deploy.arn
    api_deploy      = aws_iam_role.api_deploy.arn
    frontend_deploy = aws_iam_role.frontend_deploy.arn
  }
}
