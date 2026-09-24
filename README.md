# <img src="frontend/favicon-180.png" alt="" height="32" valign="middle"> OpenTechJobs

Open-source tech job board. Scrapes job listings directly from ATS APIs and company career portals, tracks them over time, and serves them through a lightweight public API.

**Live site:** [opentechjobs.org](https://opentechjobs.org/) (board at [/board](https://opentechjobs.org/board), API docs at [/api/help](https://opentechjobs.org/api/help))

**License:** MIT

![The job board on a laptop, standing in a sunlit field](frontend/img/showcase-desktop.webp)

## Architecture

The core data store is a SQLite (`jobs.db`) database running on a single
t4g.medium EC2 instance (il-central-1), continuously replicated to Amazon
S3 via Litestream.

`ARCHITECTURE.md` serves as the authoritative source for system design,
failure modes, and operational runbooks.

```
  scrapers                   box (EC2 t4g.medium)              readers
  ────────                   ────────────────────              ───────

  GitHub Actions ─┐
  daily, slow     │          ┌──────────────────────────┐
  guesses ATS     ├─ S3 ────►│ fetch_fragments.py (30s) │
  tokens          │  delta   │ shadow_apply.py  (timer) │
                  │  frags   ├──────────────────────────┤
  EventBridge ────┘          │ jobs.db  ~3GB  WAL       │──► Litestream ──► S3
  Lambda, 5 min              ├──────────────────────────┤
  re-polls known             │ publish.py       (timer) │──► artifacts ──► S3
  company/ATS pairs          ├──────────────────────────┤
  browser ──► Cloudflare ───►│ gunicorn -w 2 --threads 4│
              Worker         │   api/handler.py (WSGI)  │
                             └──────────────────────────┘
```

### Design rationale

**Stateless polling, stateful ingestion.** Light HTTP probing runs on AWS
Lambda to leverage low-cost, parallel execution (94% of polls return
`304 Not Modified`). Database ingestion and query serving run on the EC2
instance, which maintains local state and page cache for the ~3 GB
database.

**Concurrency control.** All write-heavy operations on the host
(`shadow_apply.py`, `publish.py`, snapshots) acquire a file lock
(`box/lock.py`) to prevent SQLite write contention.

### Core components

**1. Ingestion pipeline**

`scrape_handler.py` (Lambda, 5-min schedule): Executes `probe.py` using
`If-None-Match` headers across active ATS endpoints (Greenhouse, Lever,
Ashby, SmartRecruiters). Writes delta fragments to S3 without database
contact.

*Adaptive backoff:* Active boards poll every 5 minutes. Inactive boards
back off exponentially up to 4 hours, resetting to 5 minutes immediately
upon detecting new postings.

`scrape_workday_handler.py` (Lambda, hourly): Handles heavy and custom
targets pinned in `companies.yml` (Workday, custom corporate portals,
Oracle, Eightfold).

`box/fetch_fragments.py` and `box/shadow_apply.py` (EC2 timers):
`fetch_fragments.py` copies S3 fragments to a local spool every 30
seconds. `shadow_apply.py` processes the spool in 48 MB passes. The
decoupling prevents race conditions between Lambda fragment deletion and
EC2 ingestion cycles.

**2. API and routing**

`api/handler.py` (WSGI): Served via `gunicorn -w 2 --threads 4` reading
directly from `jobs.db`.

Routing via Cloudflare Worker (`infra/cloudflare-worker.js`):

```
/api/auth/*                 ──► CloudFront ──► AWS Lambda        sign-in flows
/api/*, /job/*, /company/*  ──► cloudflared ──► EC2 (:8000)
/*                          ──► CloudFront ──► S3                static assets
```

*Rollback path:* The legacy Lambda stack remains deployed. Removing the
Cloudflare Worker routes restores full Lambda-based execution.

**3. Discovery engine**

GitHub Actions (daily): Scans the Common Crawl URL index, verifies
endpoints, populates `domains.txt`, and resolves corporate identities.

### Storage and data strategy

**Optimized FTS indexing.** Job descriptions are stored individually in
S3 and retrieved on demand. Raw JSON payloads are discarded, while search
relies on a contentless SQLite FTS5 index (~1.8 GB of the ~3 GB database
footprint).

**Latency profile.** Active jobs reach the site within ~5 minutes of
posting. Ingestion onto EC2 occurs within 1–2 minutes of fragment
arrival.

## Authentication

Public job listings require no auth. Optional saved filters and email
alerts use AWS Cognito backed by Google OAuth, a custom Lambda flow for
GitHub OAuth, and passwordless Email OTP.

## Repository structure

| Path / file | Purpose |
|---|---|
| `probe.py` | Core ATS scraping engine supporting conditional HTTP requests. |
| `companies.yml` | Declarative pins for Workday, Comeet, and custom portal configurations. |
| `domains.txt` | Target domain resolution list. |
| `db/schema.sql` | DDL schema, including the contentless FTS5 virtual table. |
| `loader/` | Utilities for S3 description blobs (`descriptions.py`), deltas (`deltas.py`), and SQLite upserts (`load_to_sqlite.py`). |
| `box/` | Host-side daemons: fragment fetcher, applier, static publisher, and execution locking. |
| `api/` | WSGI application source code for the core API endpoints. |
| `alerts.py` | Saved search evaluation and email dispatch daemon. |
| `frontend/` | Framework-less vanilla UI. Four pages (`index.html`, `board.html`, `account.html`, `contact.html`), one `style.css`, and `app.js` with `auth.js`, `account.js` and `cv_skills.js` beside it. |
| `infra/` | Terraform manifests managing AWS resources and Cloudflare routing. |
| `ARCHITECTURE.md` | The current system in full, including failure modes. The authority. |
| `.github/workflows/` | CI/CD pipelines (discovery, `deploy-infra.yml`, `deploy-api.yml`, `deploy-frontend.yml`). |
