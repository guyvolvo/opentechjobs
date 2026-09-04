# OpenMarketIL

Open-source Israeli tech job board. Scrapes job postings directly from
ATS APIs (no careers-page fingerprinting; see `probe.py`'s module
docstring), tracks them over time, and serves them through a small public
API. No accounts, no paywall, no alerting tier: the API itself is the
alerting primitive; poll it yourself or see `examples/` (TODO) for a
Telegram alerter.

## Architecture

```
GitHub Actions (compute + CI/CD)
  scrape-discover.yml, once/day    (slow: guessing + Comeet/embed scraping)
    probe.py --batch domains.txt --json > resolved.json
    loader/load_to_sqlite.py  -->  upserts jobs.db + re-derives known.json
                                    -->  both pushed to S3
                                    (companies.yml pins Comeet uid/token
                                     pairs that can't be guessed)

  scrape-fast.yml, every 10 min    (fast: re-poll already-known boards only)
    probe.py --known known.json --json > resolved.json   (no guessing at all)
    loader/load_to_sqlite.py  -->  same upsert, same S3 push

  deploy-frontend.yml, on push to frontend/**
    aws s3 sync frontend/ -> frontend bucket, --delete
    aws cloudfront create-invalidation                    (else viewers keep
                                                             the cached build)

AWS (serving only; see infra/)
  S3 (data bucket)        jobs.db, read by Lambda on cold start
  Lambda + Function URL   api/handler.py: GET /jobs, /companies, /stats
  S3 (frontend bucket)    static site: frontend/index.html, style.css, app.js
  CloudFront              one domain: /api/* -> Lambda, else -> frontend S3
  GitHub OIDC             4 scoped roles, no long-lived AWS keys in GH secrets
```

Why two scrape workflows instead of one: discovering a company's ATS
(guessing tokens, scraping its careers page for Comeet/Workday/embedded-
ATS links, falling back to JobPosting JSON-LD) is expensive, measured
~15 min over ~350 domains, but a company's ATS platform essentially
never changes minute to minute, so there's nothing to gain from
re-running that discovery often, only cost. Re-polling a board whose
ats+token is *already known* is a single direct API call, ~4s for 123
companies measured, so that's the piece that actually runs "as up to
date as possible" (every 10 min; see scrape-fast.yml's header for why not
tighter). `loader.py` re-derives `known.json` on every load, discovery or
fast-poll alike, so anything newly discovered is available to the very
next fast-poll run, not stuck behind the next daily discovery cycle.

Why SQLite-in-S3 instead of RDS: the write pattern is a full batch
upsert from a single GitHub Actions run at a time, not concurrent
transactional writes, and reads are a low-volume public API. That's a
near-$0/mo setup (Lambda + S3 free tiers cover it) against RDS's
unavoidable ~$12-15/mo floor once its free tier ends. Concurrent
multi-writer access would need a real migration, not a config change;
this project hasn't needed it.

Why this DB is current-state-only, not history: jobs.db exists purely to
serve "what does the board look like right now" cheaply. `first_seen` /
`last_seen` / `closed_at` on each job row give a job's own lifetime (age,
and whether the current posting is a repost) without needing a separate
historical store; see `db/schema.sql`. A time-series view (daily/monthly
snapshots, queried client-side) is future scope, not built here.

## First-time deploy

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

From here, `scrape-discover.yml` runs unattended once/day,
`scrape-fast.yml` every 10 min, and `deploy-infra.yml` / `deploy-api.yml` /
`deploy-frontend.yml` handle changes to `infra/`, `api/`, and `frontend/`
respectively.

## Repo layout

| Path | What |
|---|---|
| `probe.py` | ATS discovery + scraping, ground-truthed against live Greenhouse/Lever/Ashby/Workable/Recruitee/SmartRecruiters/Comeet APIs. `--selftest`, `--verbose`, `--raw URL` for debugging. |
| `companies.yml` | Hand/scrape-verified Comeet uid+token pins. Comeet can't be guessed from a domain the way the others can. |
| `domains.txt` | The company domains probed each run. |
| `db/schema.sql` | jobs.db schema. |
| `loader/load_to_sqlite.py` | resolved.json -> jobs.db, upserted (not wiped), optional S3 push. |
| `api/` | The serving Lambda. See `api/README.md` for local testing without AWS credentials. |
| `frontend/` | Static site: `index.html` + `style.css` + `app.js`, no build step, no framework. Talks to `/api/*` same-origin (behind CloudFront). Starring a job is localStorage-only, no accounts. |
| `scripts/dev_server.py` | Local preview: serves `frontend/` and proxies `/api/*` to `api/handler.py` in-process against a local `jobs.db`, no AWS needed. `python scripts/dev_server.py --db jobs.db`. |
| `infra/` | Terraform. `infra/bootstrap/` is the one-time state-bucket setup, applied separately. |
| `.github/workflows/` | `scrape-discover.yml` (daily, slow), `scrape-fast.yml` (every 10 min, fast), `deploy-infra.yml`, `deploy-api.yml`, `deploy-frontend.yml`. |

## Not built yet

- Automated discovery: finding new company domains via Common Crawl or
  crt.sh. Still a manual research process. This is different from the
  best-effort *scraper* tier (JobPosting JSON-LD, `confidence:
  best_effort`), which exists and runs today for domains that miss every
  known ATS API; see `probe.py`'s `f_embed_scrape`.
- A historical trend view (client-side "how has this looked over time"),
  e.g. via periodic snapshots queried from the frontend. jobs.db covers
  current-state only; see the architecture note above.
