# Issued in us-east-1 via the aliased provider, required by CloudFront.
# DNS validation record is added manually in Cloudflare (this domain's
# nameservers are locked there, so DNS stays there rather than moving to
# Route 53, also avoids Route 53's ~$0.50/mo hosted-zone charge).
resource "aws_acm_certificate" "site" {
  provider = aws.us_east_1

  domain_name = var.domain_name
  # The redirect-source domain (see cloudfront.tf's redirect Function)
  # needs to be on the same distribution's cert too, or CloudFront never
  # accepts it as an alias in the first place.
  subject_alternative_names = concat(var.legacy_domain_names, var.alias_domain_names)
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# No validation_record_fqdns: polls ACM directly for ISSUED status
# instead of checking Route53-created records that don't exist here.
resource "aws_acm_certificate_validation" "site" {
  provider = aws.us_east_1

  certificate_arn = aws_acm_certificate.site.arn
}

# A second certificate, for the hosted sign-in domain. Separate from the
# site's rather than another name on it: adding one there revalidates
# the certificate CloudFront is currently serving, and there is no
# reason to touch a working distribution to move a user pool.
#
# us-east-1 like the other, and for a different reason: Cognito requires
# a custom domain's certificate there whatever region the pool is in.
resource "aws_acm_certificate" "auth" {
  provider = aws.us_east_1

  domain_name       = var.auth_domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "auth" {
  provider = aws.us_east_1

  certificate_arn = aws_acm_certificate.auth.arn
}
