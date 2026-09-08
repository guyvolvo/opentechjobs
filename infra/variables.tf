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
  default     = 2048
  description = "Was 256 (\"SQLite reads on a ~2MB DB are light\") until 2026-09-08: jobs.db grew to ~197MB via the overnight Common-Crawl merge, and /api/stats' own heavy aggregates (median age, ghost-job rate, 14-day daily history) started timing out outright at 10s against a DB nearly 100x the size this Lambda was sized for. Bumped 1024->2048 the same day after jobs.db grew again, to 639MB in one large discovery-pipeline batch (Max Memory Used was already 700-720MB against the smaller cached version) -- real margin above a number that's proven it can jump hundreds of MB in a single merge, not just today's usage. More memory also means more CPU/network allocation in Lambda, directly helping the query speed itself, not just headroom."
}

variable "lambda_timeout_s" {
  type        = number
  default     = 25
  description = "Was 10. Bumped to 25, not higher: API Gateway v2 (HTTP API, see infra/apigateway.tf) has a hard 29-30s integration timeout that Terraform can't raise -- a Lambda timeout past that ceiling would just mean API Gateway itself cuts the request instead, no better outcome. If real query duration keeps growing past this as the merge queue keeps landing, the fix is making /api/stats' own aggregates cheaper (pre-computed, not live on every request), not another timeout bump into a wall that doesn't move."
}

variable "scrape_fast_memory_mb" {
  type        = number
  default     = 1024
  description = "Re-sharded 2026-09-08 (see scrape_handler.py's own docstring): this Lambda used to re-poll ALL known companies every 5 minutes, and its memory (512->1024->2048->3008MB over one incident) kept chasing that growing full-DB footprint, on a trajectory toward $30-50+/month as the now-continuously-refilling discovery pipeline kept adding companies -- a real user budget ceiling (<$5/month, all AWS services combined) made 'keep raising the ceiling' unworkable. Sharding into fixed ~50-company batches (SHARD_SIZE) and moving VACUUM out to scrape_maintenance_handler.py decouples this Lambda's per-invocation cost from total company count, so 1024MB is real headroom above what a single small shard needs, not a number chasing yesterday's OOM. Verify against real CloudWatch Max Memory Used after the first few live shard cycles rather than trusting this blind."
}

variable "scrape_fast_timeout_s" {
  type        = number
  default     = 120
  description = "Ceiling for probe.py --known against ONE shard (~50 companies, see SHARD_SIZE) plus the no-vacuum SQLite upsert -- comfortably more than a shard this size needs (a 358-company full run measured well under 90s before sharding even existed), left generous because the real cost driver is memory x duration, not this ceiling."
}

variable "scrape_workday_memory_mb" {
  type        = number
  default     = 3008
  description = "Workday's dedicated Lambda handles a small, hand-pinned company set (companies.yml), but load_to_sqlite.py's own pull-modify-push cycle still downloads and opens the FULL, ever-growing jobs.db regardless of how few rows this run touches -- unlike scrape_fast, sharding this Lambda wouldn't help, since the memory pressure comes from total DB size, not company count. Confirmed live (2026-09-08): jobs.db passed 795MB and this Lambda started hitting Runtime.OutOfMemory outright at the 1024MB this variable used to be (which had been sized against a real measurement of 760-764MB against a much smaller DB, already stale by the time it mattered). 3008MB, the account's real Lambda memory ceiling (see scrape_fast_memory_mb's own history for how that number was found), not a guess at how much further this specific number will need to grow -- this Lambda has no sharding to decouple it from that growth, so watch it again."
}

variable "scrape_workday_timeout_s" {
  type        = number
  default     = 120
  description = "A real run measured 51-55s with VACUUM included; --skip-vacuum should only shorten that. 120s is margin, not a number chasing a measured failure."
}

variable "scrape_maintenance_memory_mb" {
  type        = number
  default     = 3008
  description = "Once-daily VACUUM pass (scrape_maintenance_handler.py) against the full jobs.db -- the one place VACUUM still runs at all, after 2026-09-08 moved it out of both frequent re-poll cycles to stop paying its whole-file-rewrite cost on every 5-20 minute cycle. Generous on purpose: at 30 invocations/month this is a rounding error in the monthly GB-second budget regardless of how high this number is, so there's no reason to right-size it as tightly as the frequent Lambdas above -- better to have real headroom for a growing jobs.db than to relearn the OOM/disk-full lessons from earlier the same day."
}

variable "scrape_maintenance_timeout_s" {
  type        = number
  default     = 240
  description = "Generous for the same reason as the memory variable above -- once a day, cost-negligible regardless, no benefit to cutting this close."
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
