variable "aws_region" {
  type    = string
  default = "il-central-1"
}

variable "project_name" {
  type    = string
  default = "iljobs"
}

variable "data_bucket_name" {
  type        = string
  description = "S3 bucket for jobs.db + raw resolved.json + Parquet snapshots. Globally unique; set your own."
  default     = "iljobs-data-876913698688"
}

variable "frontend_bucket_name" {
  type        = string
  description = "S3 bucket for the static frontend. Globally unique; set your own."
  default     = "iljobs-frontend-876913698688"
}

variable "github_repo" {
  type        = string
  description = "GitHub repo in \"org/name\" form. Scopes the OIDC trust policy so only this repo's Actions runs can assume the deploy roles."
  default     = "guyvolvo/opentechjobs"
}

# GitHub appends immutable owner/repo IDs to the OIDC token's `sub` claim
# (e.g. "guyvolvo@92536827", "openmarketil@1356856304" instead of the
# plain names) so a renamed or deleted-and-recreated repo can't silently
# inherit another repo's trust policy. Confirmed via CloudTrail against
# this repo's actual failed AssumeRoleWithWebIdentity calls -- the plain
# "org/name" form never matches. Find these for a different repo from a
# failed run's CloudTrail event, or GET /repos/{owner}/{repo} (id) and
# GET /users/{owner} (id) via the GitHub API.
variable "github_owner_id" {
  type        = string
  description = "Numeric GitHub user/org id for the owner in github_repo."
  default     = "92536827"
}

variable "github_repo_id" {
  type        = string
  description = "Numeric GitHub repo id for github_repo."
  default     = "1356856304"
}

variable "github_deploy_branch" {
  type        = string
  description = "Branch allowed to assume the broad infra-deploy role. Data/frontend deploys use their own narrower roles, allowed from any branch; see iam_oidc.tf."
  default     = "main"
}

variable "lambda_memory_mb" {
  type        = number
  default     = 256
  description = "SQLite reads on a ~2MB DB are light; 256MB keeps cold starts fast without paying for headroom this workload doesn't use."
}

variable "lambda_timeout_s" {
  type    = number
  default = 10
}

variable "scrape_lambda_memory_mb" {
  type        = number
  default     = 1024
  description = "The scrape-fast Lambda re-polls every known board and upserts into a growing SQLite DB, heavier than the read-only API Lambda's workload. Bumped from 512 the same day a run was measured at 510/512MB used (Comeet's re-poll upserting a 100MB+ jobs.db) -- real OOM risk, not headroom. Lambda's network throughput scales with memory too, so this also helps the timeout margin below, not just safety."
}

variable "scrape_lambda_timeout_s" {
  type        = number
  default     = 280
  description = "Ceiling for probe.py --known (all boards, in parallel) plus the SQLite upsert. Was 120 until 2026-09-08: the overnight Common-Crawl merge (merge-discovered-companies.yml) grew known.json from 260 to 358+ companies, and every fast-poll cycle started hitting probe.py's own 90s subprocess timeout and erroring outright for 5.5 hours straight before anyone noticed -- the SAME failure shape as the 2026-09-05 incident this comment already used to warn about, just from company-count growth instead of Workday's per-job detail fetches. 280, not just enough to clear today's count: the merge queue can still add up to ~520 more companies, and this needs real headroom for that, not another repeat of the same lesson. Watch actual Duration in CloudWatch before trusting any number here again -- this project has now hit this exact wall twice."
}

variable "domain_name" {
  type        = string
  description = "Custom domain for the CloudFront distribution (site at /, API at /api/*). DNS lives in Cloudflare, not Terraform; see infra/acm.tf for the manual validation-record step."
  default     = "opentechjobs.org"
}

variable "legacy_domain_name" {
  type        = string
  description = "The project's old domain. Kept as a second CloudFront alias (see cloudfront.tf's redirect Function) so old links/bookmarks land on domain_name instead of 404ing -- never used as SITE_ORIGIN/callback URLs, those all point at domain_name only."
  default     = "openmarket.guyvoloshin.com"
}

# Auth (Cognito). Google/GitHub credentials come from each provider's own
# console, not Terraform -- no default, set via a gitignored terraform.tfvars
# (see infra/README or ask Claude; *.tfvars is already gitignored). Empty
# string is a valid, working default: the Google identity provider resource
# in cognito.tf is conditional on this being set, so the pool applies fine
# without it and Google sign-in can be wired in later without disrupting
# anything already live.
variable "google_client_id" {
  type        = string
  description = "OAuth client ID from Google Cloud Console (APIs & Services > Credentials), once the Cognito domain below exists to give it a redirect URI."
  default     = ""
}

variable "google_client_secret" {
  type        = string
  description = "OAuth client secret paired with google_client_id."
  default     = ""
  sensitive   = true
}

# GitHub doesn't federate with Cognito directly (no OIDC discovery
# document, no id_token from its OAuth token endpoint) -- these drive a
# custom Lambda-based auth flow instead, not a Cognito identity provider
# resource. See github_auth_lambda.tf.
variable "github_oauth_client_id" {
  type        = string
  description = "Client ID from a GitHub OAuth App (github.com/settings/developers). Callback URL: https://<domain_name>/api/auth/github/callback."
  default     = ""
}

variable "github_oauth_client_secret" {
  type        = string
  description = "Client secret paired with github_oauth_client_id."
  default     = ""
  sensitive   = true
}

variable "alerts_from_email" {
  type        = string
  description = "SES sender address for alert digests. Must be on a domain verified in alerts_ses.tf (DNS records added manually in Cloudflare, same pattern as acm.tf)."
  default     = "alerts@guyvoloshin.com"
}
