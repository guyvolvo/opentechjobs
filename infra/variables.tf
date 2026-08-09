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
  description = "S3 bucket for jobs.db + raw resolved.json + Parquet snapshots. Globally unique -- set your own."
  default     = "iljobs-data-CHANGE-ME"
}

variable "frontend_bucket_name" {
  type        = string
  description = "S3 bucket for the static frontend. Globally unique -- set your own."
  default     = "iljobs-frontend-CHANGE-ME"
}

variable "github_repo" {
  type        = string
  description = "GitHub repo in \"org/name\" form. Scopes the OIDC trust policy so only this repo's Actions runs can assume the deploy roles."
  default     = "CHANGE-ME/il-jobs"
}

variable "github_deploy_branch" {
  type        = string
  description = "Branch allowed to assume the infra-deploy role (broad permissions). Data uploads (scrape-fast.yml, scrape-discover.yml) and frontend syncs (deploy-frontend.yml) are allowed from any branch via their own narrower roles -- see iam_oidc.tf."
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
