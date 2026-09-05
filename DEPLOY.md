# Self-hosting

Requires Terraform >=1.10, the AWS CLI, and an AWS account you can
authenticate to locally at least once.

1. **Pick unique names.** S3 bucket names are global. Replace every
   `CHANGE-ME` in `infra/bootstrap/main.tf`, `infra/versions.tf`
   (`backend "s3" { bucket = ... }`), and `infra/variables.tf`
   (`data_bucket_name`, `frontend_bucket_name`, `github_repo`).

2. **Bootstrap the Terraform state bucket** (one-time, local state;
   see that file's header comment for why this can't go through CI):
   ```
   cd infra/bootstrap
   terraform init
   terraform apply
   ```

3. **First real apply, locally** (the GitHub OIDC roles this project
   uses for CI don't exist until this runs once):
   ```
   cd infra
   terraform init
   terraform apply
   ```

4. **Wire up GitHub Actions.** `terraform output github_actions_role_arns`
   gives you four ARNs. In the repo's Settings -> Actions -> Variables
   (not Secrets: these aren't sensitive), set:
   - `INFRA_DEPLOY_ROLE_ARN`, `DATA_DEPLOY_ROLE_ARN`, `API_DEPLOY_ROLE_ARN`,
     `FRONTEND_DEPLOY_ROLE_ARN` from that output
   - `DATA_BUCKET` = `terraform output data_bucket`
   - `LAMBDA_FUNCTION_NAME` = `terraform output lambda_function_name`
   - `FRONTEND_BUCKET` = `terraform output frontend_bucket`
   - `CLOUDFRONT_DISTRIBUTION_ID` = `terraform output cloudfront_distribution_id`

5. **Verify.** `terraform output cloudfront_domain`, then:
   ```
   curl https://<that domain>/api/health
   ```
   should return `{"ok": true}`. `/api/stats` will show zero jobs until
   `scrape-discover.yml` runs once (push to main, or trigger it manually
   from the Actions tab) and pushes the first `jobs.db` + `known.json`.
   `scrape-fast.yml` has nothing to do until that first `known.json`
   exists; it exits early rather than erroring (see its own header).
   The site itself won't appear until `deploy-frontend.yml` runs once
   too (push to main, or trigger it manually). The frontend bucket
   starts out empty, Terraform only provisions it.

From here, `scrape-discover.yml` runs unattended once/day, the
EventBridge-scheduled `scrape-fast` Lambda every 5 min (`scrape-fast.yml`
itself is workflow_dispatch-only, see its own header comment), and
`deploy-infra.yml` / `deploy-api.yml` / `deploy-frontend.yml` handle
changes to `infra/`, `api/`, and `frontend/` respectively.

If you rename the GitHub repo (or transfer it to a different owner) after
setup, the OIDC trust policy breaks silently: GitHub's token `sub` claim
is ID-suffixed (`owner@id/repo@id`, not the plain name), and those IDs
don't change on a rename, but `github_owner_id`/`github_repo_id` in
`infra/variables.tf` still need to match whatever `github_repo` is set to
now. Update both and re-apply.
