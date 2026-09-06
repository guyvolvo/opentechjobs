# Opentechjobs.org

Open-source Israeli tech job board. Scrapes job postings directly from ATS APIs, tracks them over time, and serves them through a small public API.

Live at [opentechjobs.org](https://opentechjobs.org) (the old `openmarket.guyvoloshin.com` redirects there).

for alerting options see `examples/` (TODO) for a Telegram alerter example.

## Architecture

```
GitHub Actions (compute + CI/CD)

scrape-discover.yml, once/day discovery scraper that tries to find new job boards

probe.py --batch domains.txt --json > resolved.json
loader/load_to_sqlite.py > upserts jobs.db + re-derives known.json

both pushed to S3

scrape_handler.py, an EventBridge-scheduled Lambda that re-polls existing job boards every 5 min (scrape-fast.yml itself is workflow_dispatch-only now, see its own header comment)
```

Auth is separate and optional -- everything above needs no login at all. The alerts feature (save a filter, get a digest email on new matches) sits behind Cognito: Google OAuth, GitHub (via a custom Lambda auth flow, GitHub has no OIDC discovery document so it can't be a plain Cognito identity provider), and anonymous email one-time-codes. See `infra/cognito.tf`, `infra/github_auth_lambda.tf`, `github_auth_handler.py`, and `alerts.py` (runs once per fast-poll cycle, matches each alert's filter against newly-seen jobs).

Since budget was the the primary limitation for this project I went with an SQLite DB in S3 instead of RDS which costs basically nothing

## Repo layout

| File | Purpose |
|---|---|
| `probe.py` | ATS discovery + scraping, against known ATS APIs. `--selftest`, `--verbose`, `--raw URL` for debugging. |
| `companies.yml` | scrape-verified Comeet/Workday pins |
| `domains.txt` | The known resolved company domains |
| `db/schema.sql` | jobs.db schema. |
| `loader/load_to_sqlite.py` | resolved.json -> jobs.db,  optional S3 push. |
| `api/` | The serving Lambda. See `api/README.md` |
| `alerts.py` | Saved-filter email alerts, run once per fast-poll cycle. |
| `github_auth_handler.py` | GitHub/email-OTP sign-in, Cognito's custom-auth Lambda. |
| `frontend/` | Static site: `index.html` + `style.css` + `app.js`,  backend is`/api/*`|
| `scripts/dev_server.py` | spins up a local dev server |
| `infra/` | Terraform backend. `infra/bootstrap/` one-time state-bucket setup |
| `.github/workflows/` | has both the slow and fast scrapers, `deploy-infra.yml`, `deploy-api.yml`, `deploy-frontend.yml`. |

## Future agenda

- Automated discovery: finding new company domains via Common Crawl or crt.sh.
- A historical trend view e.g. via periodic snapshots queried from the frontend.
