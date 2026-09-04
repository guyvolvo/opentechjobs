-- OpenMarketIL live-serving schema (SQLite).
--
-- This is a CURRENT-STATE store, not a time-series one: upserted every
-- load (see loader/load_to_sqlite.py) against whatever probe.py + the
-- deep scraper found, so first_seen/last_seen/closed_at survive across
-- runs. It answers "what does the board look like right now," not "how
-- did it trend." That's Parquet's job, not this DB's.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
    domain          TEXT PRIMARY KEY,
    ats             TEXT,               -- greenhouse|personio|lever|ashby|workable|recruitee|
                                         -- smartrecruiters|comeet|workday|jsonld|NULL (miss)
    token           TEXT,               -- ats-specific token, or "uid:token" for comeet
    confidence      TEXT,               -- 'verified' | NULL (miss). See jobs.confidence note;
                                         -- pinned and guessed tokens are equally fresh, both
                                         -- hit the live API every run
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
    posted_at           TEXT,              -- ISO 8601 from the ATS, NULL if unreported
    description_chars   INTEGER NOT NULL DEFAULT 0,
    description         TEXT,              -- cleaned plain text (see probe.py's _clean_text), NULL
                                            -- where the ATS's list endpoint has no description at
                                            -- all (SmartRecruiters, Comeet, Workday). Powers keyword search.
    seniority           TEXT,              -- intern|junior|mid|senior|staff|principal|lead|manager|
                                            -- director|exec|NULL. Structured ATS field when one
                                            -- exists, else a title-keyword guess. NULL is common:
                                            -- most titles state no level.
    workplace_type      TEXT,              -- remote|hybrid|onsite|NULL. Structured ATS field when
                                            -- one exists, else a location-text guess. NULL is
                                            -- common; plenty of postings just don't say.

    confidence          TEXT NOT NULL,     -- 'verified' (direct ATS API response) | 'best_effort'
                                            -- (deep scraper, JSON-LD or heuristic DOM scrape).
                                            -- Never blend best_effort silently into verified counts.

    first_seen          TEXT NOT NULL,     -- ISO 8601, first load this job id appeared in
    last_seen           TEXT NOT NULL,     -- ISO 8601, most recent load it was still present
    closed_at           TEXT,              -- ISO 8601, set when a load no longer sees this id.
                                            -- Reappearing with a new posted_at after closing is a
                                            -- repost signal.

    raw_json            TEXT               -- full fetched record, for reprocessing without a re-scrape
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_domain);
CREATE INDEX IF NOT EXISTS idx_jobs_posted_at ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS idx_jobs_confidence ON jobs(confidence);
CREATE INDEX IF NOT EXISTS idx_jobs_closed_at ON jobs(closed_at);
CREATE INDEX IF NOT EXISTS idx_jobs_seniority ON jobs(seniority);
CREATE INDEX IF NOT EXISTS idx_jobs_workplace_type ON jobs(workplace_type);
-- No index on `description`: only ever queried via leading-wildcard LIKE,
-- which can't use a btree index anyway. A few thousand rows is a cheap
-- sequential scan; FTS5 would be the answer at an order of magnitude more.

-- One row, updated every load, lets the API report "as of" without a
-- separate metadata channel.
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
