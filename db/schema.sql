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
                                         -- smartrecruiters|jazzhr|teamtailor|comeet|workday|
                                         -- jsonld|NULL (miss)
    token           TEXT,               -- ats-specific token, or "uid:token" for comeet
    confidence      TEXT,               -- 'verified' | NULL (miss). See jobs.confidence note;
                                         -- pinned and guessed tokens are equally fresh, both
                                         -- hit the live API every run
    job_count       INTEGER NOT NULL DEFAULT 0,
    tried           INTEGER NOT NULL DEFAULT 0,   -- probe attempts made, for debugging hit rate
    error           TEXT,
    first_seen      TEXT NOT NULL,      -- ISO 8601, first time this domain was probed at all
    last_checked    TEXT NOT NULL,      -- ISO 8601, most recent probe run
    -- The company's own name as its ATS reports it ("Headout", "Informa
    -- Group Plc."), not derived from `domain`. Exists because `domain`
    -- is frequently NOT the company's real hostname: discovery guesses
    -- {ats-token}.com and, when that doesn't resolve, keeps the guess
    -- anyway (see refresh_discovery_queue.py). Roughly a fifth of a
    -- 40-company sample were invented that way, so the board was
    -- captioning real listings "headoutcareers.com" when the company is
    -- Headout. NULL until resolved, and callers fall back to `domain`.
    company_name    TEXT,
    -- Whether `domain` was ever confirmed to resolve. 0 means treat it
    -- as an internal id only: don't show it as an identity and don't
    -- fetch a logo from it.
    domain_verified INTEGER
);

CREATE INDEX IF NOT EXISTS idx_companies_ats ON companies(ats);

-- Full-text index over job descriptions.
--
-- content='' makes this "contentless": FTS5 stores the inverted index
-- and never the original text. That is the whole point. Descriptions are
-- ~94% of the snapshot's bytes (a job row's metadata is 570 bytes, the
-- full row averages 9,521), which is what made jobs-read.db 1.2GB and
-- would make it 8.9GB at a million listings, past the 10GB Lambda /tmp
-- ceiling every reader has to fit inside. The text itself now lives as
-- one S3 object per job (loader/descriptions.py); the words stay here so
-- they remain searchable.
--
-- Keyed by jobs.rowid. A contentless table cannot return columns, only
-- match rowids, which is exactly what build_jobs_where's keywords filter
-- needs: `jobs.rowid IN (SELECT rowid FROM jobs_fts WHERE ... MATCH ?)`.
-- That also replaces a LIKE '%term%' scan over every row with a real
-- index, so keyword search gets faster as well as cheaper.
--
-- rowid is only stable within one database file, so this is populated
-- wherever jobs rows are written (load_to_sqlite on ingest) and rebuilt
-- from scratch whenever they are re-created (merge_partitions), never
-- copied between files.
-- contentless_delete=1 (SQLite 3.43+) is what makes an UPDATE possible.
-- A plain contentless table can only remove a row if handed back the
-- exact string it indexed, and the snapshot deliberately no longer
-- stores that string, so the old terms could never be removed: a job
-- whose description changed stayed matchable by BOTH its old and new
-- text, forever, silently. Caught by test rather than in production.
-- open_db falls back to a plain contentless table if the runtime is
-- older, and index_description handles both.
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    description,
    content='',
    contentless_delete=1
);

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
    skills              TEXT,              -- comma-joined, up to 5 tech/skill terms matched against
                                            -- title+description (see probe.py's _extract_skills).
                                            -- Empty/NULL for non-technical roles or no description.
    salary_text         TEXT,              -- real disclosed comp (currently Ashby only) or an
                                            -- Israel role x seniority market estimate, never both --
                                            -- see salary_is_estimate. NULL where neither applies.
    salary_is_estimate  INTEGER NOT NULL DEFAULT 0,  -- 0/1. 1 means salary_text is probe.py's
                                            -- _estimate_salary(), not the listing's own disclosed
                                            -- figure -- the frontend must render these differently.

    description_sha     TEXT,              -- fingerprint of `description`, so a load can tell an
                                            -- unchanged description from a changed one without
                                            -- reading its S3 blob back. Without it every load would
                                            -- rewrite every blob: ~1,600 PUTs per shard run and
                                            -- ~460,000 a day for no benefit. See loader/descriptions.py.

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
