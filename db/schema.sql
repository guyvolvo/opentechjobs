-- IL/JOBS live-serving schema (SQLite).
--
-- This DB is the CURRENT-STATE store behind the API -- it gets rebuilt
-- from scratch every load (see loader/load_to_sqlite.py) from whatever
-- probe.py + the deep scraper found this run, upserted against what was
-- already here so first_seen/last_seen/closed_at survive across runs.
--
-- This is deliberately NOT the historical time-series store. The
-- original brief's git-snapshot + Parquet + DuckDB-WASM design already
-- covers "how has this looked over time" (daily/monthly snapshots as
-- GitHub Release assets, queried client-side). This DB exists to serve
-- the live API cheaply (SQLite in S3, loaded into Lambda) -- it answers
-- "what does the board look like right now," not "how did it trend."
-- Don't grow this into a second history mechanism; that's what Parquet
-- is for.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
    domain          TEXT PRIMARY KEY,
    ats             TEXT,               -- greenhouse | personio | lever | ashby | workable
                                         -- | recruitee | smartrecruiters | comeet | workday
                                         -- | jsonld (best-effort, see jobs.confidence) | NULL (miss)
    token           TEXT,               -- ats-specific token, or "uid:token" for comeet
    confidence      TEXT,               -- 'verified' | NULL (miss) -- see note on jobs.confidence;
                                         -- a companies.yml-pinned token is just as fresh as a
                                         -- guessed one since resolve() still hits the live API
                                         -- for it every run, so there's no separate 'pinned' tier
    job_count       INTEGER NOT NULL DEFAULT 0,
    tried           INTEGER NOT NULL DEFAULT 0,   -- probe attempts made, for debugging hit rate
    error           TEXT,
    first_seen      TEXT NOT NULL,      -- ISO 8601, first time this domain was probed at all
    last_checked    TEXT NOT NULL       -- ISO 8601, most recent probe run
);

CREATE INDEX IF NOT EXISTS idx_companies_ats ON companies(ats);

CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,     -- stable hash: domain + url/external_id (see loader)
    company_domain      TEXT NOT NULL REFERENCES companies(domain),
    ats                 TEXT NOT NULL,
    external_id         TEXT,
    title               TEXT NOT NULL,
    location            TEXT,
    department          TEXT,
    url                 TEXT,
    posted_at           TEXT,              -- ISO 8601 from the ATS, NULL if the ATS didn't report one
    description_chars   INTEGER NOT NULL DEFAULT 0,
    description         TEXT,              -- cleaned/truncated plain text (see probe.py's _clean_text),
                                            -- NULL where the ATS's list endpoint doesn't include
                                            -- description content at all (SmartRecruiters, Comeet,
                                            -- Workday -- confirmed live, would need a per-job detail
                                            -- request to get it). Powers /jobs?keywords=.
    seniority           TEXT,              -- intern|junior|mid|senior|staff|principal|lead|manager|
                                            -- director|exec|NULL (no signal). Structured ATS field
                                            -- when one exists (SmartRecruiters, Comeet), else a
                                            -- title-keyword guess (see probe.py's _classify_seniority).
                                            -- NULL is the common case -- most titles state no level.
    workplace_type      TEXT,              -- remote|hybrid|onsite|NULL (no signal). Structured ATS
                                            -- field when one exists (Ashby/Lever/SmartRecruiters/
                                            -- Recruitee/Comeet), else a location-text guess (see
                                            -- probe.py's _classify_workplace). NULL is common --
                                            -- plenty of postings just don't say.

    confidence          TEXT NOT NULL,     -- 'verified' | 'best_effort'
                                            -- verified = a direct ATS API response (token guessed
                                            -- or pinned, doesn't matter -- both hit the live API
                                            -- every run). best_effort = deep scraper (JobPosting
                                            -- JSON-LD or heuristic DOM scrape) against a domain that
                                            -- misses every known ATS API. Lower trust -- never blend
                                            -- silently into 'verified' counts in the API/UI.

    first_seen          TEXT NOT NULL,     -- ISO 8601, first load this job id appeared in
    last_seen           TEXT NOT NULL,     -- ISO 8601, most recent load it was still present
    closed_at           TEXT,              -- ISO 8601, set when a load no longer sees this id --
                                            -- (last_seen - first_seen) is the listing's lifetime;
                                            -- a NULL-closed_at row reappearing with a new posted_at
                                            -- after being closed is a repost signal

    raw_json            TEXT               -- full fetched record, for reprocessing without a re-scrape
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_domain);
CREATE INDEX IF NOT EXISTS idx_jobs_posted_at ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS idx_jobs_confidence ON jobs(confidence);
CREATE INDEX IF NOT EXISTS idx_jobs_closed_at ON jobs(closed_at);
CREATE INDEX IF NOT EXISTS idx_jobs_seniority ON jobs(seniority);
CREATE INDEX IF NOT EXISTS idx_jobs_workplace_type ON jobs(workplace_type);
-- No index on `description`: it's only ever queried via LIKE '%term%'
-- (keyword search, leading wildcard), which can't use a btree index
-- anyway. A few thousand rows is a cheap sequential scan; SQLite FTS5
-- would be the answer if this dataset grew an order of magnitude past
-- that, not before.

-- One row, updated every load; lets the API report "as of" without a
-- separate metadata channel and lets Lambda's staleness check work off
-- the DB file itself rather than trusting S3 object metadata alone.
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
