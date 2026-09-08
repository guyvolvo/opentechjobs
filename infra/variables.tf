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
  default     = 1024
  description = "Was 256 (\"SQLite reads on a ~2MB DB are light\") until 2026-09-08: jobs.db grew to ~197MB via the overnight Common-Crawl merge, and /api/stats' own heavy aggregates (median age, ghost-job rate, 14-day daily history) started timing out outright at 10s against a DB nearly 100x the size this Lambda was sized for. More memory also means more CPU/network allocation in Lambda, directly helping the query speed itself, not just headroom."
}

variable "lambda_timeout_s" {
  type        = number
  default     = 25
  description = "Was 10. Bumped to 25, not higher: API Gateway v2 (HTTP API, see infra/apigateway.tf) has a hard 29-30s integration timeout that Terraform can't raise -- a Lambda timeout past that ceiling would just mean API Gateway itself cuts the request instead, no better outcome. If real query duration keeps growing past this as the merge queue keeps landing, the fix is making /api/stats' own aggregates cheaper (pre-computed, not live on every request), not another timeout bump into a wall that doesn't move."
}

variable "scrape_lambda_memory_mb" {
  type        = number
  default     = 3008
  description = "The scrape-fast Lambda re-polls every known board and upserts into a growing SQLite DB, heavier than the read-only API Lambda's workload. Bumped 512->1024 once already (a run measured 510/512MB used, Comeet's re-poll upserting a 100MB+ jobs.db); 1024->2048 on 2026-09-08 after the same overnight company-count growth pushed a real run to Runtime.OutOfMemory at 1024MB. Tried 2048->4096 the same day given a real run at 358 known companies already measured 1367-1384MB (67% of 2048) with the discovery pipeline (discover-companies.yml + merge-discovered-companies.yml, now running continuously rather than a one-time batch) projected to more than double that count -- rejected live: il-central-1 (or this account) caps Lambda MemorySize at 3008MB regardless of the usual 10,240MB ceiling (confirmed via a real UpdateFunctionConfiguration ValidationException, not documentation), so 3008 is the actual ceiling available, not a choice. If real Duration/memory usage climbs close to this as the queue keeps draining, the next lever is a Service Quotas increase request for this limit, not another number here. Lambda's network throughput scales with memory too, so this also helps the timeout margin below, not just safety. Shared with scrape_workday_lambda.tf's own function -- that one only handles a dozen companies and was nowhere near either wall, so this is more headroom than it strictly needs, but not worth a second variable just to avoid over-provisioning a Lambda this cheap to run either way."
}

variable "scrape_lambda_timeout_s" {
  type        = number
  default     = 400
  description = "Ceiling for probe.py --known (all boards, in parallel) plus the SQLite upsert. Was 120 until 2026-09-08: the overnight Common-Crawl merge grew known.json from 260 to 358+ companies, and every fast-poll cycle started hitting probe.py's own 90s subprocess timeout and erroring outright for 5.5 hours straight before anyone noticed -- the SAME failure shape as the 2026-09-05 incident this comment already used to warn about, just from company-count growth instead of Workday's per-job detail fetches. Bumped 280->400 the same day alongside the memory variable above: company discovery is now a standing, continuously-running pipeline (discover-companies.yml refills the queue daily, merge-discovered-companies.yml drains it every 10 minutes) rather than a one-time overnight batch, so the company count keeps climbing indefinitely rather than leveling off at some known final number -- watch actual Duration in CloudWatch periodically rather than treating any fixed number here as permanent. This project has now hit this exact wall twice already from undersizing for growth that was already in motion."
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
