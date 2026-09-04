output "cloudfront_domain" {
  value       = aws_cloudfront_distribution.main.domain_name
  description = "CloudFront's own domain, still works after the custom domain is live (aliases are additive). Also the CNAME target for var.domain_name in Cloudflare."
}

output "acm_validation_records" {
  description = "CNAME to add in Cloudflare (DNS-only / grey-cloud) to validate the certificate. Leave it in place permanently, ACM re-checks it for renewals."
  value = {
    for dvo in aws_acm_certificate.site.domain_validation_options : dvo.domain_name => {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  }
}

output "api_gateway_invoke_url" {
  value       = aws_apigatewayv2_stage.api.invoke_url
  description = "Direct API Gateway URL, bypassing CloudFront. Useful for debugging cache issues"
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

output "scrape_lambda_function_name" {
  value = aws_lambda_function.scrape_fast.function_name
}

output "cloudfront_distribution_id" {
  value       = aws_cloudfront_distribution.main.id
  description = "For deploy-frontend.yml's CreateInvalidation call. CLOUDFRONT_DISTRIBUTION_ID repo variable."
}

# Paste into the repo's Settings -> Actions -> Variables (not secrets;
# role ARNs carry no key material) so workflows know which role to assume.
output "github_actions_role_arns" {
  value = {
    infra_deploy         = aws_iam_role.infra_deploy.arn
    data_deploy          = aws_iam_role.data_deploy.arn
    api_deploy           = aws_iam_role.api_deploy.arn
    scrape_lambda_deploy = aws_iam_role.scrape_lambda_deploy.arn
    frontend_deploy      = aws_iam_role.frontend_deploy.arn
  }
}
