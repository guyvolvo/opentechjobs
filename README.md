# <img src="frontend/favicon-180.png" alt="" height="32" valign="middle"> OpenTechJobs

Open-source tech job board. Scrapes job listings directly from ATS APIs and company career portals, tracks them over time, and serves them through a lightweight public API.

**Live site:** [opentechjobs.org](https://opentechjobs.org/) (board at [/board](https://opentechjobs.org/board), API docs at [/api/help](https://opentechjobs.org/api/help))

**License:** MIT

![The job board on a laptop, standing in a sunlit field](frontend/img/showcase-desktop.webp)

## Architecture

To run on a near-zero AWS budget, the system uses an S3-hosted SQLite database (`jobs-read.db`) instead of an always-on RDS instance.

The pipeline uses two EventBridge Lambdas on 5-minute schedules. Polling and writing are decoupled because polling is cheap, whereas persisting updates directly used to require a 48MB pull-modify-push per partition.

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
  └── (Hourly) ─► scrape_workday_handler.py ────► S3: deltas/*.json
```

### Components

**`scrape_handler.py` (every 5 min).** Runs `probe.py` against boards due on the current tick using `If-None-Match` HTTP headers. ~94% of polls return `304 Not Modified` with no payload (Greenhouse, Lever, Ashby, SmartRecruiters). Writes small delta files (`deltas/{ts}-{run}.json`) for companies with changed listings. Never touches the database directly.

Polling order is driven by observed activity (`loader/scrape_state.py`). A board that recently posted is polled every 5 minutes. Quiet boards back off gradually (5, 8, 11, 17, 25, 38, 57, 85, 128, 192 minutes) up to a 4-hour ceiling, resetting to 5 minutes immediately upon posting. Errors maintain their interval without backing off.

**`scrape_maintenance_handler.py` (every 5 min).** Replays pending delta fragments into `jobs-read.db`, uploads the updated database to S3, and deletes fragments only after a successful push. Evaluates saved-filter alerts and rebuilds `bootstrap.json`.

**`scrape_workday_handler.py` (hourly).** Polls large or slow targets: Workday tenants pinned in `companies.yml`, custom company portals (Amazon, Microsoft, Google, Apple, Check Point), and pinned platforms (Oracle Recruiting Cloud, Eightfold, WP Job Openings). Pins can specify custom cadences via `every_hours`.

**`api/handler.py` (behind Cloudflare → CloudFront → API Gateway).** Reads `jobs-read.db` from `/tmp`, refreshed via lightweight `HEAD` checks.

**Discovery workflow (daily via GitHub Actions).** Queries the Common Crawl URL index → verifies endpoints → outputs `domains.txt` → resolves company names.

## Storage and freshness

### Storage optimizations

The snapshot remains small enough to download into a Lambda's `/tmp` on every refresh:

- Job descriptions are stored as individual S3 objects and fetched on demand.
- Raw JSON payloads are removed entirely.
- Search runs on a contentless SQLite FTS5 index.

Live counts, file sizes, and snapshot versions are reported dynamically via `/api/health` and `/api/stats`.

### Freshness and edge cases

**Listing age vs. refresh age.** Listing age is when the employer posted the role. Refresh age is when the pipeline last polled that employer.

**Propagation delay.** A new job reaches the live site within ~5 minutes of being polled. Active boards are checked every 5 minutes; quiet boards take up to 4 hours. Merging takes up to 5 minutes, and the API serves the new snapshot within 1 minute after that.

**Unreachable employers.** Employers that block anonymous access or return empty responses (e.g., IBM Avature, Amdocs/Qualcomm Eightfold) are recorded as unresolved rather than mapped to guessed endpoints.

## Authentication

All job board search and API endpoints require no authentication.

The optional Alerts feature (saving filters and receiving email digests) uses AWS Cognito:

- **Google OAuth**
- **GitHub OAuth:** custom Lambda authorization flow (required because GitHub lacks an OIDC discovery endpoint).
- **Email one-time password (OTP):** custom Lambda authentication flow for passwordless sign-in.

## Repository structure

| Path / file | Purpose |
|---|---|
| `probe.py` | ATS discovery and scraping engine using conditional requests. Includes `--selftest`, `--verbose`, and `--raw` CLI flags for debugging. |
| `companies.yml` | Explicit Comeet, Workday, and custom portal pins. |
| `domains.txt` | List of resolved company domains. |
| `db/schema.sql` | Database schema, including the contentless FTS5 virtual table. |
| `loader/load_to_sqlite.py` | Converts `resolved.json` into `jobs-read.db` with an optional S3 upload. |
| `loader/deltas.py` | Delta fragment store utilities (write, list, read, delete). |
| `loader/descriptions.py` | Manages description blobs in S3. Hash-gated to prevent duplicate PUT operations. |
| `loader/bootstrap.py` | Generates `bootstrap.json` for frontend prerendering. |
| `api/` | API Lambda implementation. See `api/README.md`. |
| `alerts.py` | Evaluates saved filters and sends notification emails. |
| `scrape_maintenance_handler.py` | 5-minute pipeline applier: merges delta fragments into `jobs-read.db`. |
| `scrape_workday_handler.py` | Hourly Workday and custom portal scraper. |
| `dispatch_workflow_handler.py` | Triggers GitHub workflows that bypass GitHub Cron schedules. |
| `resolve_company_names.py` | Maps internal board tokens to real company names. |
| `github_auth_handler.py` | Custom Cognito authentication Lambda for GitHub OAuth and Email OTP. |
| `frontend/` | Static frontend (`index.html`, `style.css`, `app.js`). Calls `/api/*`. |
| `scripts/dev_server.py` | Local development server. |
| `infra/` | Terraform configuration. `infra/bootstrap/` provisions the initial state bucket. |
| `.github/workflows/` | CI/CD pipelines (discovery, `deploy-infra.yml`, `deploy-api.yml`, `deploy-frontend.yml`). |
