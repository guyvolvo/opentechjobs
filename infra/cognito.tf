# Auth for the alerts feature only -- everything else on this site stays
# fully public, no login required. Three sign-in paths, no passwords
# anywhere in the system:
#   - Google: standard Cognito-federated OAuth redirect.
#   - Anonymous email: Cognito's native EMAIL_OTP first-factor, no SES
#     needed for this path (Cognito sends the code itself, genuinely $0).
#   - GitHub: NOT a Cognito identity provider -- GitHub has no OIDC
#     discovery document and its OAuth token endpoint never returns an
#     id_token, so Cognito's OIDC provider type can't federate with it
#     directly. Driven instead by a custom Lambda auth flow
#     (github_auth_lambda.tf) using this pool's CUSTOM_AUTH challenge
#     triggers. See that file for the actual flow.

resource "aws_cognito_user_pool" "main" {
  name = "${var.project_name}-users"

  # Essentials tier (not the classic Lite/default tier) is required for
  # sign_in_policy -- EMAIL_OTP as a first auth factor doesn't exist
  # below it. Still within Cognito's free allotment at this project's
  # expected scale.
  user_pool_tier = "ESSENTIALS"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  sign_in_policy {
    # Cognito's CreateUserPool API rejects a sign_in_policy that omits
    # PASSWORD ("Password should be configured as one of the allowed
    # first auth factors", confirmed live) -- it has to be structurally
    # allowed even though it's never actually usable in practice: every
    # user in this pool (GitHub-linked or EMAIL_OTP) gets its password
    # set via AdminSetUserPassword to a random, immediately-discarded
    # value that no human ever learns, and the frontend never shows a
    # password field or a PASSWORD sign-in option at all. Structurally
    # present, practically dead.
    allowed_first_auth_factors = ["PASSWORD", "EMAIL_OTP"]
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
  }

  # GitHub-linked users are created via AdminCreateUser with a
  # deterministic username (github_<id>), not a real password -- so
  # there's no user-facing password to enforce a policy on. Left at
  # Cognito's own default rather than special-cased; nothing in this
  # system ever prompts a human to type a password.

  admin_create_user_config {
    allow_admin_create_user_only = true # only github_auth_lambda.tf calls AdminCreateUser; no public self-registration bypass
  }

  # One Lambda (github_auth_lambda.tf) handles all three custom-auth
  # trigger sources, dispatching on event.triggerSource -- simpler than
  # three separate functions for what's really one flow.
  lambda_config {
    define_auth_challenge          = aws_lambda_function.github_auth.arn
    create_auth_challenge          = aws_lambda_function.github_auth.arn
    verify_auth_challenge_response = aws_lambda_function.github_auth.arn
  }
}

# Classic prefix domain (*.auth.<region>.amazoncognito.com), not a custom
# domain: a custom Cognito domain needs its own ACM cert (us-east-1,
# separate from the one in acm.tf, which is CloudFront's) plus another
# manual DNS step. The prefix domain is free, is all Google's OAuth
# redirect actually needs, and the sign-in UI itself is custom-built in
# the frontend regardless -- this domain is invisible to a user, it only
# ever appears mid-redirect.
resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${var.project_name}-auth-876913698688"
  user_pool_id = aws_cognito_user_pool.main.id
}

# Conditional on google_client_id being set (see variables.tf) so the
# pool applies cleanly before the Google Cloud Console credentials exist;
# add them later and re-apply, no disruption to anything already live.
resource "aws_cognito_identity_provider" "google" {
  count         = var.google_client_id != "" ? 1 : 0
  user_pool_id  = aws_cognito_user_pool.main.id
  provider_name = "Google"
  provider_type = "Google"

  provider_details = {
    client_id        = var.google_client_id
    client_secret    = var.google_client_secret
    authorize_scopes = "openid email profile"
  }

  attribute_mapping = {
    email    = "email"
    username = "sub"
  }
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.project_name}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret = false # called directly from browser JS, not a confidential client

  explicit_auth_flows = [
    "ALLOW_USER_AUTH",   # EMAIL_OTP, via sign_in_policy above
    "ALLOW_CUSTOM_AUTH", # GitHub, via github_auth_lambda.tf's triggers
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # "Google" only gets added once the identity provider above actually
  # exists (see the conditional count on it); COGNITO stays for the
  # EMAIL_OTP/CUSTOM_AUTH paths, which authenticate against the pool's
  # own user directory, not a federated one.
  supported_identity_providers = concat(
    ["COGNITO"],
    var.google_client_id != "" ? ["Google"] : []
  )

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]

  # The root, not a dedicated /auth/callback path: this is a static site
  # with no server-side routing, so any path other than "/" 404s at
  # CloudFront/S3 -- app.js checks location.search for ?code= on every
  # load instead of needing a second real page.
  callback_urls = ["https://${var.domain_name}/"]
  logout_urls   = ["https://${var.domain_name}/"]

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}
