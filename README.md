# OpenMarketIL

Open-source Israeli tech job board. Scrapes job postings directly from ATS APIs , tracks them over time, and serves them through a small public API.
for alerting options see `examples/` (TODO) for a Telegram alerter example.

## Architecture

```
GitHub Actions (compute + CI/CD)

scrape-discover.yml, once/day discovery scraper that tries to find new job boards

probe.py --batch domains.txt --json > resolved.json

loader/load_to_sqlite.py > upserts jobs.db + re-derives known.json

both pushed to S3

scrape-fast.yml, a quicker scrape that runs every 10 min to find job postings in existing job boards
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

Since budget was the the primary limitation for this project I went with an SQLite DB in S3 instead of RDS which costs basically nothing

## Repo layout

| File | Purpose |
|---|---|
| `probe.py` | ATS discovery + scraping, against known ATS APIs. `--selftest`, `--verbose`, `--raw URL` for debugging. |
| `companies.yml` | scrape-verified Comeet uid+token pins |
| `domains.txt` | The known resolved company domains |
| `db/schema.sql` | jobs.db schema. |
| `loader/load_to_sqlite.py` | resolved.json -> jobs.db,  optional S3 push. |
| `api/` | The serving Lambda. See `api/README.md` |
| `frontend/` | Static site: `index.html` + `style.css` + `app.js`,  backend is`/api/*`|
| `scripts/dev_server.py` | spins up a local dev server |
| `infra/` | Terraform backend. `infra/bootstrap/` one-time state-bucket setup |
| `.github/workflows/` | has both the slow and fast scrapers, `deploy-infra.yml`, `deploy-api.yml`, `deploy-frontend.yml`. |

## Future agenda

- Automated discovery: finding new company domains via Common Crawl or crt.sh.
- A historical trend view e.g. via periodic snapshots queried from the frontend.
