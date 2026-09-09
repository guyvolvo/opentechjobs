# Opentechjobs.org

Open-source Israeli tech job board. Scrapes job postings directly from ATS APIs, tracks them over time, and serves them through a small public API.

Live at [opentechjobs.org](https://opentechjobs.org) (the old `openmarket.guyvoloshin.com` redirects there). MIT-licensed; see the [privacy policy](https://opentechjobs.org/privacy.html) for what the alerts feature collects.

Roughly 3,450 companies, 113,000 open listings, and a new posting reaches the site about five minutes after the company publishes it. The whole thing runs inside the AWS free tier. Budget was the hard constraint from day one, which is why the database is a SQLite file in S3 rather than RDS.

## Architecture

Two Lambdas on five-minute EventBridge schedules do all the recurring work. One polls, one writes. Keeping those separate is the single most important decision in here, and it took a few failed designs to arrive at.

```
sweep (Lambda, every 5 min)
  probe.py polls all ~3,450 companies
  conditional GET: If-None-Match / If-Modified-Since
  ~88% answer 304 with no body
  writes ONE delta fragment to s3://.../deltas/{ts}-{run}.json
  holding only the companies whose listings actually changed

apply (Lambda, every 5 min)
  reads pending fragments, oldest first
  replays them into jobs-read.db, incrementally, then pushes it
  deletes the fragments only after that push succeeds
  evaluates saved-filter alerts against the new snapshot
  rebuilds bootstrap.json for the frontend's first paint

serve
  Cloudflare -> CloudFront -> API Gateway -> api/handler.py
  which reads jobs-read.db out of /tmp, refreshed on a HEAD check
```

### Why polling and writing are split

They scale differently, and pretending otherwise cost this project a week.

Polling every company got cheap once conditional requests went in. Greenhouse, Lever, Ashby and SmartRecruiters all honor `If-None-Match`, which covers about 88% of tracked companies, so most of a sweep is bodiless 304s. Workable, Comeet, Recruitee and Workday do not, so those still pay for a full fetch every time.

Writing was the part that would not scale. The database used to be split into partitions, one per shard of companies, and persisting a sweep meant a 48MB pull-modify-push for every partition it touched, at 50 to 170 seconds each. A sweep that wanted 18 partitions finished one and threw the rest away. Widening the sweep made that strictly worse.

The fix was to stop writing databases from the sweep at all. A delta fragment is a small JSON file holding only the companies that changed, so a sweep is kilobytes in steady state and never opens SQLite. Fragments are named by timestamp so they sort chronologically, which matters because two sweeps can carry the same company and the later one has to win. They survive until a snapshot containing them is safely pushed, so a crash costs a repeat rather than a listing. Replaying is harmless, since the loader is an upsert.

### What made the snapshot small enough to move every 5 minutes

`jobs-read.db` was 1.2GB, which is why the merge took 70 seconds and could only run hourly. Three changes took it under 400MB.

Descriptions moved out to `descriptions/{job_id}.json`, one object per listing, written only when a hash on the row says the text actually changed. The API reads them back on demand in the detail endpoint. That was 42% of the file, and `raw_json`, dropped outright, was another 48%.

Search survived that on a contentless FTS5 index (`content=''`, `contentless_delete=1`), which keeps the inverted index and never stores the text. It needs SQLite 3.43 or newer.

The applier then stopped rebuilding. Pulling a 300MB snapshot, applying a few kilobytes and pushing it back beats downloading every partition and merging from scratch, and it is what makes a five-minute cadence affordable.

### The rest of the schedule

`scrape_workday_handler.py` re-polls Workday-pinned companies every 30 minutes on its own connection pool, isolated from the fleet's. Workday cannot be guessed the way Greenhouse and Lever can, so its tenants are hand-pinned in `companies.yml`, along with per-tenant Israel location facet ids. Those facets are per-deployment, with no shared id across tenants. NVIDIA's id silently no-ops on Cisco's rather than erroring.

`dispatch_workflow_handler.py` fires GitHub Actions workflows over the API on a 10-minute rule and a daily one. It exists because GitHub's own `schedule:` cron is not reliable on this repo. A cron tightened to every 10 minutes sat five hours stale while manual dispatches kept working, which is a long-standing known issue ([community discussion 147369](https://github.com/orgs/community/discussions/147369)). EventBridge has an actual SLA, so anything that must happen on time is triggered from there and the workflow keeps `workflow_dispatch` only.

Discovery runs daily. `discover_companies.py` searches Common Crawl's URL index for each ATS's public board host pattern, `merge_discovered_batch.py` folds verified finds into `domains.txt`, and `resolve_company_names.py` turns board tokens into real display names by reading each ATS's own board metadata.

## Data in S3

| Key | What it is |
|---|---|
| `jobs-read.db` | The live snapshot. The only file the API reads. |
| `deltas/{ts}-{run}.json` | Pending sweep results, deleted once applied. |
| `descriptions/{job_id}.json` | One description per listing, read through by the detail endpoint. |
| `known.json` | Every resolved company and its ATS token. The sweep's input. |
| `status.json`, `merge-status.json` | Separate files on purpose. Sharing one meant the applier's "merging" phase got overwritten seconds later by the next sweep. |
| `bootstrap.json` | In the frontend bucket, not this one. The first page of listings, prerendered, so the board paints from the edge in about 40ms instead of waiting on the API. The frontend ignores it unless its recorded params match the current filters exactly. |

## Auth

Optional, and entirely separate. Everything above works with no login.

Alerts (save a filter, get a digest when new listings match) sit behind Cognito: Google OAuth, GitHub through a custom Lambda auth flow, and anonymous email one-time codes. GitHub needs the custom flow because it publishes no OIDC discovery document, so it cannot be a plain Cognito identity provider. See `infra/cognito.tf`, `infra/github_auth_lambda.tf`, `github_auth_handler.py`, and `alerts.py`.

## Repo layout

| File | Purpose |
|---|---|
| `probe.py` | ATS discovery and scraping. Owns conditional requests and the `unchanged` state. `--selftest`, `--verbose`, `--raw URL` for debugging. |
| `sharding.py` | The domain to shard map, shared so the writers and the merge cannot disagree about it. |
| `companies.yml` | Hand-verified Comeet and Workday pins, including Workday Israel facet ids. |
| `domains.txt` | Known resolved company domains. |
| `db/schema.sql` | Snapshot schema, including the contentless FTS5 table. |
| `loader/load_to_sqlite.py` | Results to SQLite, upsert, optional S3 push. |
| `loader/deltas.py` | The delta fragment store. Write, list, read, delete. |
| `loader/descriptions.py` | Description blobs in S3, hash-gated so unchanged text costs no PUT. |
| `loader/bootstrap.py` | Builds `bootstrap.json`. |
| `loader/merge_partitions.py` | The old partition merge. Still used by `migrate_to_partitions.py`, no longer in the live path. |
| `api/` | The serving Lambda. See `api/README.md`. |
| `api/job_filters.py` | Filters to SQL, and the FTS5 branch behind them. |
| `api/help_page.py` | The rendered API reference at `/api/help`. |
| `alerts.py` | Saved-filter email alerts, evaluated once per apply. |
| `scrape_handler.py` | The 5-minute sweep. |
| `scrape_maintenance_handler.py` | The 5-minute applier. |
| `scrape_workday_handler.py` | Workday's own 30-minute re-poll. |
| `dispatch_workflow_handler.py` | Triggers the GitHub workflows that GitHub's own cron will not. |
| `discover_companies.py`, `refresh_discovery_queue.py`, `merge_discovered_batch.py` | The Common Crawl discovery pipeline. |
| `resolve_company_names.py` | Board tokens to real company names. |
| `backfill_descriptions.py` | One-time. Copied 92,667 existing descriptions to S3 before the column was dropped. |
| `github_auth_handler.py` | GitHub and email-OTP sign-in, Cognito's custom-auth Lambda. |
| `frontend/` | Static site. `index.html`, `style.css`, `app.js`. See `DESIGN.md`. |
| `scripts/dev_server.py` | Local dev server. |
| `infra/` | Terraform. `infra/bootstrap/` is the one-time state bucket setup. |
| `.github/workflows/` | Deploys, discovery, and the manual-dispatch scrapers. |

## Known loose ends

The Workday Lambda still writes `jobs-partition-workday.db` and nothing reads it any more. Workday companies reach the snapshot through the global sweep instead, which their live listings confirm, so this is dead weight rather than a data gap. It should be deleted.

The snapshot drifts upward. It sits near 370MB against roughly 284MB for a clean rebuild, because the applier writes incrementally and free pages accumulate. A periodic VACUUM would settle it, at the cost of a full-file rewrite.

Sustained polling at about 11 requests per second across Ashby and Greenhouse has not triggered rate limiting yet. `probe.py` backs off on 429 and 503, but that ceiling is untested.

Google sign-in is built and switched off. It needs a Google Cloud OAuth client, then `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` as repository secrets, then flipping `GOOGLE_CONFIGURED` in `app.js`.

## Future agenda

- A historical trend view, from periodic snapshots queried by the frontend.
- Company logos served from our own store rather than a third-party favicon service.
