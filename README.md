# <img src="frontend/favicon-180.png" alt="" height="32" valign="middle"> OpenTechJobs

Open-source tech job board. Scrapes job listings directly from ATS APIs, tracks them over time, and serves them through a lightweight public API.

![The job board on a laptop and a phone](frontend/img/readme-cover.webp)

## Architecture

To stay within a near-zero AWS budget, the system runs on an S3-hosted SQLite database (`jobs-read.db`) instead of an always-on RDS instance.

The pipeline uses two EventBridge Lambdas running on 5-minute schedules. Polling and writing are decoupled because polling is cheap, whereas persisting updates directly used to require a 48MB pull-modify-push per partition.

```
[EventBridge]
  │
  ├── (5 min) ──► scrape_handler.py ─────────────► S3: deltas/*.json
  │                                                   │
  ├── (5 min) ──► scrape_maintenance_handler.py ◄─────┘
  │                    │
  │                    ├──► S3: jobs-read.db
  │                    ├──► S3: bootstrap.json
  │                    └──► Evaluate Email Alerts
  │
  └── (30 min) ─► scrape_workday_handler.py ────► S3: deltas/*.json
```

### Components

**`scrape_handler.py` (Every 5 min):** Runs `probe.py` to poll ~3,450 companies using `If-None-Match` HTTP headers. ~88% of requests return a `304 Not Modified` with no payload (Greenhouse, Lever, Ashby, SmartRecruiters). Writes small delta files (`deltas/{ts}-{run}.json`) for companies with changed listings. Never touches the database directly.

**`scrape_maintenance_handler.py` (Every 5 min):** Replays pending delta fragments into `jobs-read.db` incrementally, uploads the updated database to S3, and deletes fragments only after a successful push. Evaluates saved-filter alerts and rebuilds `bootstrap.json`.

**`scrape_workday_handler.py` (Every 30 min):** Polls Workday tenants explicitly pinned in `companies.yml`.

**`api/handler.py` (Behind Cloudflare → CloudFront → API Gateway):** Reads `jobs-read.db` from `/tmp`, refreshed via lightweight HEAD checks.

**Discovery Workflow (Daily via GitHub Actions):** Queries the Common Crawl URL index → Verifies endpoints → Outputs `domains.txt` → Resolves company names.

### Storage Optimizations

Database size is ~370MB (down from 1.2GB):

- Job descriptions are stored as individual S3 objects and fetched on-demand.
- Raw JSON payloads were removed entirely.
- Search runs on a contentless SQLite FTS5 index.

A new job reaches the live site within ~5 minutes.

## Authentication

All job board search and API endpoints require no authentication.

The optional Alerts feature (saving filters and receiving email digests) uses AWS Cognito:

- **Google OAuth**
- **GitHub OAuth:** Custom Lambda authorization flow (required because GitHub lacks an OIDC discovery endpoint).
- **Email One-Time Password (OTP):** Custom Lambda authentication flow for passwordless sign-in.

## Repository Structure

| Path / File | Purpose |
|---|---|
| `probe.py` | ATS discovery and scraping engine using conditional requests. Includes `--selftest`, `--verbose`, and `--raw` CLI flags for debugging. |
| `companies.yml` | Explicit Comeet and Workday configuration pins. |
| `domains.txt` | List of resolved company domains. |
| `db/schema.sql` | Database schema, including the contentless FTS5 virtual table. |
| `loader/load_to_sqlite.py` | Converts `resolved.json` into `jobs-read.db` with an optional S3 upload. |
| `loader/deltas.py` | Delta fragment store utilities (write, list, read, delete). |
| `loader/descriptions.py` | Manages description blobs in S3. Hash-gated to prevent duplicate PUT operations. |
| `loader/bootstrap.py` | Generates `bootstrap.json` for frontend prerendering. |
| `api/` | API Lambda implementation. See `api/README.md`. |
| `alerts.py` | Evaluates saved filters and sends notification emails. |
| `scrape_maintenance_handler.py` | 5-minute pipeline applier: merges delta fragments into `jobs-read.db`. |
| `scrape_workday_handler.py` | 30-minute Workday scraper handler. |
| `dispatch_workflow_handler.py` | Triggers GitHub workflows that bypass GitHub Cron schedules. |
| `resolve_company_names.py` | Maps internal board tokens to real company names. |
| `github_auth_handler.py` | Custom Cognito authentication Lambda for GitHub OAuth and Email OTP. |
| `frontend/` | Static frontend (`index.html`, `style.css`, `app.js`). Calls `/api/*`. |
| `scripts/dev_server.py` | Local development server. |
| `infra/` | Terraform configuration. `infra/bootstrap/` provisions the initial state bucket. |
| `.github/workflows/` | CI/CD pipelines (discovery, `deploy-infra.yml`, `deploy-api.yml`, `deploy-frontend.yml`). |
