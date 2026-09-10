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
  default     = 1536
  description = "Was 256 (\"SQLite reads on a ~2MB DB are light\") until 2026-09-08: jobs.db grew to ~197MB via the overnight Common-Crawl merge, and /api/stats' own heavy aggregates (median age, ghost-job rate, 14-day daily history) started timing out outright at 10s against a DB nearly 100x the size this Lambda was sized for. Bumped 1024->2048 the same day after jobs.db grew again, to 639MB in one large discovery-pipeline batch (Max Memory Used was already 700-720MB against the smaller cached version) -- real margin above a number that's proven it can jump hundreds of MB in a single merge, not just today's usage. More memory also means more CPU/network allocation in Lambda, directly helping the query speed itself, not just headroom."
}

variable "lambda_timeout_s" {
  type        = number
  default     = 25
  description = "Was 10. Bumped to 25, not higher: API Gateway v2 (HTTP API, see infra/apigateway.tf) has a hard 29-30s integration timeout that Terraform can't raise -- a Lambda timeout past that ceiling would just mean API Gateway itself cuts the request instead, no better outcome. If real query duration keeps growing past this as the merge queue keeps landing, the fix is making /api/stats' own aggregates cheaper (pre-computed, not live on every request), not another timeout bump into a wall that doesn't move."
}

variable "scrape_fast_memory_mb" {
  type        = number
  default     = 1536
  description = "Re-sharded 2026-09-08 to decouple probe.py's own workload from total company count -- that part worked (a shard's own ATS-fetching stays cheap and fast regardless of how many companies exist overall). Missed at the time: load_to_sqlite.py's pull-modify-push cycle downloads and opens the FULL jobs.db on every single invocation no matter how small the shard is, so sharding never touched THIS Lambda's real growth exposure at all. Confirmed live the same day: jobs.db passed 795MB and every single scrape_fast invocation started failing outright (Runtime.OutOfMemory or a straight timeout) at the 1024MB/120s this had been sized to -- a live, total-pipeline outage, not a slow degradation. Bumped to 3008MB (this account's real Lambda memory ceiling) alongside the timeout bump below. This Lambda has the exact same unaddressed growth exposure as scrape_workday now -- watch both as jobs.db keeps growing; sharding was only ever half the fix. EDIT 2026-09-10: 1536, down from 3008. The OOM history above is real but it is history: this Lambda stopped opening a database when delta fragments replaced the pull-modify-push, and it now runs probe.py and nothing else. Measured over 24h, Max Memory Used peaks at 871MB, so 3008 was paying for a shape this function no longer has. 1536 keeps 76% headroom over the real peak. Lambda scales CPU with memory, so this is only a saving if the work is waiting on network rather than on itself: 3,450 mostly-304 HTTP fetches says it is, but watch Duration after this ships and put it back if the sweep gets slower rather than cheaper. Measured at 1536: 42.3s against 50.5s at 3008, peak 638MB. It got faster on less memory, which settles the question, so 1024 next. Then back up to 1280: on arm64 a sweep peaked at 891MB, and 13% headroom on the function that feeds everything else is not headroom. Peak here tracks how many boards changed, not how many were polled, so it moves. EDIT, measured over a full hour rather than a handful of runs: 1280 was a mistake. At 1536 the sweep averages 44.1s; at 1280 it averages 81.2s over 12 runs, min 66 and max 102. That is 102 GB-seconds a run against 66, so the smaller setting costs 53% MORE. Peak sits at 987MB against a 1280MB ceiling, and the likely cause is pressure at 77% of the limit rather than the CPU that comes with the memory. Back to 1536, where both the duration and the headroom are better."
}

variable "scrape_fast_timeout_s" {
  type        = number
  default     = 600
  description = "Was 120, then 200: confirmed live (2026-09-08) a full pull-modify-push cycle against jobs.db was landing at 100-120s even before accounting for OOM, so a shard's own probe.py work (a few seconds) was a rounding error next to the loader's own file I/O time. Bumped again to 600 the same day: this function's own probe.py subprocess timeout (200s) plus its loader subprocess timeout (300s, bumped after the identical 60s version hit TimeoutExpired on scrape_workday_handler.py's own equivalent call once jobs.db passed 1GB) sum to 500s worst-case -- a function timeout at or near that sum leaves no room for either subprocess to actually use its own margin. 600 is real headroom above that sum, not just above either piece alone. Timeout ceilings cost nothing by themselves (only actual Duration drives GB-second cost), so there's no reason to cut this close -- watch actual Duration in CloudWatch as jobs.db keeps growing, same as every other number in this file tonight."
}

variable "scrape_workday_memory_mb" {
  type        = number
  default     = 1024
  description = "Workday's dedicated Lambda handles a small, hand-pinned company set (companies.yml), but load_to_sqlite.py's own pull-modify-push cycle still downloads and opens the FULL, ever-growing jobs.db regardless of how few rows this run touches -- unlike scrape_fast, sharding this Lambda wouldn't help, since the memory pressure comes from total DB size, not company count. Confirmed live (2026-09-08): jobs.db passed 795MB and this Lambda started hitting Runtime.OutOfMemory outright at the 1024MB this variable used to be (which had been sized against a real measurement of 760-764MB against a much smaller DB, already stale by the time it mattered). 3008MB, the account's real Lambda memory ceiling (see scrape_fast_memory_mb's own history for how that number was found), not a guess at how much further this specific number will need to grow -- this Lambda has no sharding to decouple it from that growth, so watch it again. EDIT 2026-09-10: 1536, down from 3008. Same correction as scrape_fast_memory_mb, measured the same way: Max Memory Used peaks at 602MB over 24h against a 3008MB ceiling. This one does still pull a partition file. Measured at 1536: 60.3s against 65.0s at 3008, peak 615MB, so 1024 next on the same evidence."
}

variable "scrape_workday_timeout_s" {
  type        = number
  default     = 600
  description = "Was 120 (a real run measured 51-55s with VACUUM included), then 300 the same day after turning on probe.FETCH_FULL_DESCRIPTIONS unconditionally (fixing 'most Workday listings have no description', at the cost of a per-job detail fetch that used to be conditional across up to WORKDAY_MAX_JOBS=60 per pinned company). Bumped again to 600, confirmed live: a real run's probe+description phase alone measured ~72s, and its own loader subprocess call needed its timeout raised 60->300s after hitting TimeoutExpired outright against a jobs.db that had crossed 1GB -- 300 (function) was too close to that same 300 (loader alone), leaving no room for the probe phase on top. Timeout ceilings are free by themselves; watch actual CloudWatch Duration after this ships and tighten once jobs.db's growth curve is better understood, not before."
}

variable "scrape_maintenance_memory_mb" {
  type        = number
  default     = 2048
  description = "4096 as of 2026-09-09, up from 3008, the one function here that needed MORE rather than less. Measured over three real runs: Max Memory Used 2996MB against a 3008MB ceiling, 99.6%, with median duration up from ~30s that morning to 122s -- that curve is memory pressure, not data growth, and the endpoint is Runtime.OutOfMemory on the ONLY process that builds jobs-read.db (the file api/db.py serves every request from), which fails silently into stale data exactly like the 512MB ephemeral incident earlier. Likely CHEAPER at 4096, not just safer: Lambda scales CPU with memory, and 4GB x ~40s is 160 GB-s against 2.9GB x 122s = 358 GB-s today. Note the older claim that 3008 is this account's hard ceiling is unverified -- no Lambda memory service quota is exposed at all, and the only adjustable ones here are concurrency (10) and layer storage. If an apply ever rejects this, the fallback is capping SQLite's own page cache in merge_partitions.py rather than buying headroom. Original context: was the once-daily VACUUM pass against the full jobs.db; became the Partition & Merge design's own merge step the same day (loader/merge_partitions.py), now running hourly -- see scrape_maintenance_lambda.tf's own schedule comment. Still generous at the account's real ceiling: downloading every jobs-partition-*.db plus building+VACUUMing jobs-read.db needs real headroom, same OOM lesson as scrape_fast/scrape_workday earlier the same day, and at 3008MB regardless of invocation count this is memory-bound, not a place to right-size against frequency. EDIT 2026-09-10: 1536, down from 3008. That whole argument rested on holding the full snapshot, which halved when the duplicated description column stopped being written. Measured after: peak 1,074MB and 6.4s, against 2,998MB and 23.3s before. The OOM this was guarding against is no longer reachable at this file size. EDIT, same day: back up to 2048. Precomputing /stats and /facets happens here now, which added about 5s and 250MB to a run: peak 1,320MB against a 1536MB ceiling is 14% headroom on the only process that builds the file every request reads, and an OOM here goes stale site-wide rather than slow."
}

variable "scrape_maintenance_timeout_s" {
  type        = number
  default     = 600
  description = "Was 240 (fine at once/day, cost-negligible regardless of ceiling). Bumped to 600 alongside the move to an hourly merge: this function now sequentially downloads every jobs-partition-*.db (not concurrently -- see merge_partitions.py's own docstring on that simplification) before building jobs-read.db, so real margin matters more here than it did for a once-daily VACUUM-only run. Timeout ceilings are still free by themselves (only actual Duration drives GB-second cost) -- watch real CloudWatch Duration once this is live and tighten if partition count grows enough to make sequential downloads the real bottleneck."
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
  sensitive   = true
  description = "OAuth client secret paired with google_client_id."
  default     = ""
}

# GitHub doesn't federate with Cognito directly (no OIDC discovery
# document, no id_token from its OAuth token endpoint) -- these drive a
# custom Lambda-based auth flow instead, not a Cognito identity provider
# resource. See github_auth_lambda.tf.
variable "github_oauth_client_id" {
  type        = string
  description = "Client ID from a GitHub OAuth App (github.com/settings/developers). Callback URL: https://<domain_name>/api/auth/github/callback. Public by design -- it also ships in frontend/app.js, since the browser has to put it in the authorize URL. Only the paired secret is sensitive."
  default     = "Ov23lii8kIqDUL9aLhxh"
}

variable "github_oauth_client_secret" {
  type        = string
  sensitive   = true
  description = "Client secret paired with github_oauth_client_id. Never given a real default and never committed: deploy-infra.yml passes it as TF_VAR_github_oauth_client_secret from the GH_OAUTH_CLIENT_SECRET repository secret. Empty means GitHub sign-in stays switched off, which is the safe resting state rather than a broken one."
  default     = ""
}

variable "alerts_from_email" {
  type        = string
  description = "SES sender address for alert digests. Must be on a domain verified in alerts_ses.tf (DNS records added manually in Cloudflare, same pattern as acm.tf)."
  default     = "alerts@guyvoloshin.com"
}
