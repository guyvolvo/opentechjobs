# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Two audiences, treated as equally primary:

- **Israeli tech job seekers** browsing the board directly to find and act
  on a specific opening — search/filter by role, level, company, location,
  workplace type (remote/hybrid/onsite), then click through to the real
  ATS listing.
- **Developers and tinkerers** building on the public API directly (no
  accounts, no auth, no paywall) — the README's own framing is "no
  alerting tier — the API itself is the alerting primitive; poll it
  yourself or see `examples/` (TODO) for a Telegram alerter."

## Product Purpose

An open-source Israeli tech job board that scrapes postings directly from
ATS APIs (Greenhouse, Lever, Ashby, Workable, Recruitee, SmartRecruiters,
JazzHR, Teamtailor, Comeet, Workday — never careers-page fingerprinting as
the primary method), tracks each listing's lifetime (first seen / last seen / closed),
and serves the result through a small, fully public API plus a static
frontend. Success is a visitor finding a real, currently-open listing they
couldn't have found faster elsewhere, or a developer standing up their own
tool (alert bot, dashboard, scraper) on top of the API without asking
permission.

## Positioning

The user's own framing, recorded verbatim as the product's positioning
claim: **"THE place to be at if you want to find a job or see what is
available"** for Israeli tech, and **"the single best API for job
searchers who want to automate their search."**

The mechanism behind that claim — what a copycat aggregator built on
careers-page scraping or manual listings couldn't truthfully match:

- Every listing is ground-truthed against the company's own ATS API, not
  scraped off a rendered careers page — confidence is tiered
  (`verified`/`best_effort`) and exposed in the API, not asserted blindly.
- A job's full lifetime (`first_seen`/`last_seen`/`closed_at`) persists
  even after it closes, so dormancy/ghost-listing detection and
  reconstructed market history (open-jobs-over-time, closings) are real
  computed facts, not guesses.
- No accounts, no paywall, no rate-limit tier beyond CloudFront's edge
  cache — the API is the product surface, not a locked-down afterthought
  behind the UI.

## Operating Context

Fully serverless, near-$0/mo pipeline (see README's `## Architecture` for
the full diagram, not duplicated here):

- A slow daily discovery pass (GitHub Actions, guessing ATS tokens,
  Comeet/embed scraping for new companies) and a fast 5-minute re-poll of
  already-known company/ATS pairs (an EventBridge-scheduled Lambda —
  GitHub Actions' own schedule trigger proved unreliable, see
  scrape_handler.py's header comment). Workday companies are the one
  exception: pinned tenants only refresh on the slow daily pass, not the
  fast one — reported live, re-polling all of them every cycle was
  blowing the fast Lambda's time budget.
- Output lands in a SQLite file pushed to S3 (`jobs.db`), read by a Lambda
  behind a Function URL (`api/handler.py`) — chosen over RDS specifically
  to stay near $0/mo (write pattern is single-writer batch upsert, not
  concurrent transactional writes).
- The frontend is a static site (`frontend/`) served from S3 behind
  CloudFront, talking to `/api/*` same-origin. No build step, no
  framework, no CDN dependencies for anything — everything self-hosted or
  hand-built, including the one custom font.
- Starring a job is localStorage-only; there is no account system at all,
  by design, not as a missing feature.

## Capabilities and Constraints

- **Confidence tiers**: `verified` (matched a known ATS API) vs.
  `best_effort` (JobPosting JSON-LD fallback scraping) — the API defaults
  to `verified` only; callers opt into `best_effort`/`all` explicitly.
- **Freshness window**: a `FRESH_CLAUSE` window excludes outdated listings
  by default from both `/jobs` and every `/stats` aggregate; callers can
  opt into `include_outdated`/`include_closed`.
- **Israel scoping is a location-keyword heuristic** (`IL_KEYWORDS`), not
  a structured field — expanded over time as real unmatched location
  strings turn up in the data, never guessed in bulk.
- **Workplace type** (remote/hybrid/onsite) is structured/high-confidence
  on Ashby, Lever, SmartRecruiters, Recruitee, and Comeet; text-fallback
  only elsewhere; not available at all from Workday's list endpoint.
- **Undecided/explicitly not built yet**: automated company-domain
  discovery (Common Crawl/crt.sh) — still manual research today; the
  original brief's separate git-snapshot/Parquet/DuckDB-WASM historical
  trend store — current `jobs.db` is state-only, `first_seen`/`closed_at`
  cover a job's own lifetime without needing that separate store.
- **Data provenance rule** (durable, established across this project's
  whole build history, not just documented here): every ATS-specific
  data claim gets verified against a live HTTP request before shipping —
  never guessed or assumed from documentation alone. Nothing in the
  dataset is fabricated; an absence is shown as an absence, not papered
  over with a plausible-looking placeholder.

## Brand Commitments

- Name: **OpenTechJobs** (wordmark renders as "OpenTechJobs" + accent-color
  ".org"). Formerly "IL/JOBS", then "OpenMarketIL", during earlier development.
- Open-source, and that's load-bearing to the positioning, not incidental
  — "open" extends to the data access model (public API, no gate), not
  just the code license.

## Evidence on Hand

- Real, live scrape data via `probe.py` against the actual ATS APIs of
  every tracked company (`domains.txt`, `companies.yml` for
  hand-verified Comeet pins) — no seed/sample/placeholder company data.
- No customer testimonials, case studies, press, or usage metrics exist
  or should be fabricated or implied anywhere in product surfaces.

## Product Principles

1. Ground-truth every data claim against a live source before shipping it
   — never guess at an ATS's field shapes or a scraper's output.
2. The API is a first-class product surface, not internal plumbing behind
   the UI — document it fully, keep it unauthenticated, and design the UI
   to be reproducible by anyone polling the same endpoints.
3. Show absence honestly. A missing field, an unmatched location, a
   company with no confirmed ATS — represented as exactly that, never
   backfilled with a plausible guess.
4. Stay near-$0/mo to run. Infra choices (SQLite-in-S3 over RDS, GitHub
   Actions over a standing server) are load-bearing product decisions,
   not just implementation details.
5. No accounts, no paywall, ever — this is a constraint on the product,
   not a v1 shortcut waiting to be revisited.
