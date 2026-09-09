# Opentechjobs.org

Open-source Israeli tech job board. Scrapes job postings directly from ATS APIs, tracks them over time, and serves them through a small public API.

Live at [opentechjobs.org](https://opentechjobs.org) (the old `openmarket.guyvoloshin.com` redirects there). MIT-licensed; see [privacy policy](https://opentechjobs.org/privacy.html) for what the alerts feature collects.

## Architecture

Two EventBridge Lambdas on 5-minute schedules. One polls, one writes. They are split because polling and writing scale differently: a sweep is cheap, but persisting one used to mean a 48MB pull-modify-push per partition it touched.

```
scrape_handler.py, every 5 min
  probe.py polls all ~3,450 companies, sending If-None-Match
  ~88% answer 304 with no body (greenhouse, lever, ashby, smartrecruiters)
  writes one small delta fragment to deltas/{ts}-{run}.json holding only
  the companies whose listings changed. Never opens a database.

scrape_maintenance_handler.py, every 5 min
  replays pending fragments into jobs-read.db, incrementally, and pushes it
  deletes the fragments only after that push succeeds, so a crash costs a
  repeat rather than a listing
  then evaluates saved-filter alerts and rebuilds bootstrap.json

scrape_workday_handler.py, every 30 min
  Workday can't be guessed like the rest, its tenants are pinned in companies.yml

api/handler.py, behind Cloudflare -> CloudFront -> API Gateway
  reads jobs-read.db from /tmp, refreshed on a cheap HEAD check

discovery, daily on GitHub Actions
  Common Crawl URL index -> verify -> domains.txt -> resolve real company names
```

jobs-read.db is ~370MB, down from 1.2GB. Descriptions moved to S3 as one object per listing and are read back on demand, `raw_json` is gone, and search runs on a contentless FTS5 index. Small enough to pull, patch and push every 5 minutes, which is roughly how long a new job takes to reach the site.

Auth is separate and optional, everything above needs no login at all. The alerts feature (save a filter, get a digest email on new matches) sits behind Cognito: Google OAuth, GitHub (via a custom Lambda auth flow, GitHub has no OIDC discovery document so it can't be a plain Cognito identity provider), and anonymous email one-time-codes. See `infra/cognito.tf`, `infra/github_auth_lambda.tf`, `github_auth_handler.py`, and `alerts.py`.

Since budget was the primary limitation for this project I went with an SQLite DB in S3 instead of RDS, which costs basically nothing.

## Repo layout

| File | Purpose |
|---|---|
| `probe.py` | ATS discovery + scraping, against known ATS APIs. Owns conditional requests. `--selftest`, `--verbose`, `--raw URL` for debugging. |
| `companies.yml` | scrape-verified Comeet/Workday pins |
| `domains.txt` | The known resolved company domains |
| `db/schema.sql` | Snapshot schema, including the contentless FTS5 table. |
| `loader/load_to_sqlite.py` | resolved.json -> jobs-read.db, optional S3 push. |
| `loader/deltas.py` | The delta fragment store: write, list, read, delete. |
| `loader/descriptions.py` | Description blobs in S3, hash-gated so unchanged text costs no PUT. |
| `loader/bootstrap.py` | Builds bootstrap.json, the frontend's prerendered first page. |
| `api/` | The serving Lambda. See `api/README.md` |
| `alerts.py` | Saved-filter email alerts, run once per apply. |
| `scrape_maintenance_handler.py` | The 5-min applier: fragments -> jobs-read.db. |
| `scrape_workday_handler.py` | Workday's own 30-min re-poll, separate from `scrape_handler.py`. |
| `dispatch_workflow_handler.py` | Triggers the GitHub workflows GitHub's own cron won't fire reliably. |
| `resolve_company_names.py` | Board tokens -> real company names. |
| `github_auth_handler.py` | GitHub/email-OTP sign-in, Cognito's custom-auth Lambda. |
| `frontend/` | Static site: `index.html` + `style.css` + `app.js`, backend is `/api/*` |
| `scripts/dev_server.py` | spins up a local dev server |
| `infra/` | Terraform backend. `infra/bootstrap/` one-time state-bucket setup |
| `.github/workflows/` | discovery, `deploy-infra.yml`, `deploy-api.yml`, `deploy-frontend.yml`. |

## Future agenda

- A historical trend view e.g. via periodic snapshots queried from the frontend.
- Company logos from our own store instead of a third-party favicon service.
- Drop `jobs-partition-workday.db`, which nothing has read since the delta migration.
