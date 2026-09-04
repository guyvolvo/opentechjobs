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
  default     = "guyvolvo/openmarketil"
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
  default     = 512
  description = "The scrape-fast Lambda re-polls every known board and upserts into a growing SQLite DB, heavier than the read-only API Lambda's workload."
}

variable "scrape_lambda_timeout_s" {
  type        = number
  default     = 120
  description = "Ceiling for probe.py --known (all boards, in parallel) plus the SQLite upsert; both run well under this in practice."
}

variable "domain_name" {
  type        = string
  description = "Custom domain for the CloudFront distribution (site at /, API at /api/*). DNS lives in Cloudflare, not Terraform; see infra/acm.tf for the manual validation-record step."
  default     = "openmarket.guyvoloshin.com"
}
