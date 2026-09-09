#!/usr/bin/env python3
"""
Build/refresh jobs.db from probe.py's resolved.json (and, optionally, the
best-effort deep-scraper's results), then optionally push it to S3.

This is an UPSERT against the existing jobs.db, not a wipe-and-reload.
That's what lets first_seen/last_seen/closed_at survive across runs.

Usage:
    # local only, for testing
    python load_to_sqlite.py --resolved ../resolved.json --out jobs.db

    # with the best-effort scraper's output layered in
    python load_to_sqlite.py --resolved ../resolved.json --deep deep.json --out jobs.db

    # pull current jobs.db from S3 first (true incremental upsert), then push back
    python load_to_sqlite.py --resolved ../resolved.json --out jobs.db \\
        --bucket iljobs-data --key jobs.db

Dependencies: none beyond stdlib for local use. boto3 only if --bucket is given.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from descriptions import description_sha, put_many

SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"

# Stamped via PRAGMA user_version on every DB this loader writes (see
# open_db). Partition & Merge's own merge step (loader/merge_partitions.py)
# reads this back before touching a partition file's rows -- a mismatch
# means that partition was written by a loader version whose schema this
# merge code doesn't know how to trust (a column added/renamed/dropped
# since), and the safe move is to skip that one partition and alert, not
# crash the whole merge or silently merge mismatched rows. Bump this any
# time schema.sql or _NEW_COLUMNS changes in a way old readers couldn't
# handle.
SCHEMA_VERSION = 1

# Where description blobs go (loader/descriptions.py). Read from the
# environment rather than passed as a flag so every existing caller of
# this script picks it up without a signature change; unset simply means
# no blobs are written and the description column remains the only copy,
# which is exactly the pre-existing behaviour.
DESCRIPTIONS_BUCKET = os.environ.get("DATA_BUCKET")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_id(domain: str, ats: str, external_id: str | None, url: str | None, title: str) -> str:
    """Stable id for a job row. Prefers external_id (ATS-assigned) over url
    (can pick up tracking params) over title (last resort).
    """
    key = f"{domain}|{ats}|{external_id or url or title}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


# The exact text schema.sql uses, so the fallback strips precisely this
# and nothing else.
FTS_DELETE_OPTION = ",\n    contentless_delete=1"


def _create_schema(conn: sqlite3.Connection, sql: str) -> None:
    """Apply schema.sql, degrading the FTS table on an older SQLite.

    contentless_delete=1 needs 3.43+. Rather than hard-fail the whole
    pipeline on a runtime that predates it, fall back to a plain
    contentless table; index_description detects which one it got.
    """
    try:
        conn.executescript(sql)
    except sqlite3.OperationalError as e:
        if "contentless_delete" not in str(e):
            raise
        print(f"sqlite {sqlite3.sqlite_version} lacks contentless_delete, "
              f"falling back to a plain contentless index", file=sys.stderr)
        conn.executescript(sql.replace(FTS_DELETE_OPTION, ""))


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _create_schema(conn, SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    # See SCHEMA_VERSION's own comment. Stamped unconditionally on every
    # open, not just a fresh DB, so an existing jobs.db built before this
    # existed picks up the current version the next time anything writes
    # to it -- there's no meaningful "old" version to preserve, only "not
    # stamped yet."
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


# One-off column additions for a jobs.db predating these columns --
# schema.sql's CREATE TABLE IF NOT EXISTS is a no-op against an
# already-existing table. Checked via table_info rather than try/except
# on ALTER TABLE, since SQLite has no ADD COLUMN IF NOT EXISTS.
#
# Reported live last time (description_snippet): api/handler.py reads
# jobs.db directly and doesn't go through this function at all, so
# deploying an API change that SELECTs a column only this migration adds
# broke every /api/jobs call until jobs.db in S3 was migrated by hand.
# Apply this migration to the live S3 file BEFORE deploying the API
# change next time, not after.
_NEW_COLUMNS = {
    "description_sha": "TEXT",
    "skills": "TEXT",
    "salary_text": "TEXT",
    "salary_is_estimate": "INTEGER NOT NULL DEFAULT 0",
    "salary_source": "TEXT",
}


# Same idea as _NEW_COLUMNS above, for the companies table. Kept
# separate rather than folded in because the two tables' migrations have
# nothing to do with each other and a single dict would have to carry the
# table name on every entry.
_NEW_COMPANY_COLUMNS = {
    "company_name": "TEXT",
    "domain_verified": "INTEGER",
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, coltype in _NEW_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {coltype}")
    existing_companies = {row["name"] for row in conn.execute("PRAGMA table_info(companies)")}
    for name, coltype in _NEW_COMPANY_COLUMNS.items():
        if name not in existing_companies:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {name} {coltype}")


def _fts_supports_rowid_delete(conn: sqlite3.Connection) -> bool:
    """Whether jobs_fts can drop a row by rowid alone.

    True on SQLite 3.43+ where the table was created with
    contentless_delete=1. Probed once per connection against a rowid that
    cannot exist, so it costs nothing and changes nothing either way.
    """
    # Read the table's own definition rather than probing behaviour. A
    # DELETE against a rowid that matches nothing succeeds on a plain
    # contentless table too, so probing reports support that isn't there
    # and only fails once a row genuinely needs removing, which is the
    # worst possible moment to find out.
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs_fts'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and "contentless_delete" in (row[0] or "")


def index_description(conn: sqlite3.Connection, jid: str, new_text: str, old_text: str | None,
                      rowid_delete: bool) -> None:
    """Keep this job's row in the full-text index current.

    Two ways to retire the previous entry, and the first is much better.
    With contentless_delete=1 the row goes by rowid, which works even
    though the snapshot no longer stores the text. Without it, FTS5 needs
    the exact string it originally indexed handed back, so the caller has
    to capture the description BEFORE upsert_job overwrites the column,
    and a snapshot that dropped the column cannot delete at all: the
    stale terms would linger and the job would stay matchable by text it
    no longer contains.

    Passing the wrong text to the text-based delete does not error, it
    corrupts the index and surfaces later as "database disk image is
    malformed".
    """
    row = conn.execute("SELECT rowid FROM jobs WHERE id = ?", (jid,)).fetchone()
    if row is None:
        return
    rowid = row["rowid"]
    if rowid_delete:
        conn.execute("DELETE FROM jobs_fts WHERE rowid = ?", (rowid,))
    elif old_text:
        conn.execute("INSERT INTO jobs_fts(jobs_fts, rowid, description) VALUES('delete', ?, ?)",
                     (rowid, old_text))
    conn.execute("INSERT INTO jobs_fts(rowid, description) VALUES (?, ?)", (rowid, new_text))


def load_resolved(conn: sqlite3.Connection, resolved_path: Path,
                  drop_description: bool = False) -> set[str]:
    """Upsert probe.py's --json output. Every company in this file was
    re-checked THIS run, so companies/jobs not mentioned for a given
    domain but present in the DB from a prior run are fair game to close
    (see close_missing_jobs below), but only for domains that actually
    appear here with ats set. A domain that flips to MISS this run is left
    alone entirely: we have no fresh evidence either way, so touching its
    existing jobs would manufacture a false "closed" signal from what
    might just be a transient probe failure.

    A third answer, "unchanged", is handled before any of that: see the
    branch at the top of the loop. It means the board was checked and
    provably hasn't moved, so its jobs are deliberately absent from the
    payload rather than genuinely gone.

    Returns every domain this run covered, hit or miss -- --prune-stale's
    "is this domain still tracked at all" check (see prune_stale_companies).
    """
    data = json.loads(resolved_path.read_text(encoding="utf-8"))
    ts = now_iso()
    seen_ids_by_domain: dict[str, set[str]] = {}
    # (job_id, description) for descriptions genuinely new or changed
    # this run. Uploaded once after the transaction rather than inline
    # per job: an S3 round trip inside the row loop would dominate the
    # load, and most loads have nothing here at all.
    changed_descriptions: list[tuple[str, str]] = []
    # Probed once here rather than per job: which delete strategy the FTS
    # table supports is a property of the file, not of a row.
    rowid_delete = _fts_supports_rowid_delete(conn)

    for r in data:
        domain = r["domain"]
        ats = r.get("ats")
        token = r.get("token")

        # "unchanged": probe.py's conditional re-poll got a 304 (or the
        # normalized content hash matched), so it deliberately did NOT
        # fetch this board's jobs. That is NOT the same as a board that
        # came back empty, and conflating the two destroys live data:
        # falling through with jobs == [] puts this domain into
        # seen_ids_by_domain with an empty set, and close_missing_jobs
        # then marks every open listing at this company closed. On a
        # shard where most companies legitimately answer 304 -- which is
        # the normal case, ~88% of tracked companies are on ATSes that
        # support conditional GET -- that closes most of the board every
        # cycle. tests/test_unchanged_state.py fails loudly without this
        # branch; it was written before it and did exactly that.
        #
        # Only tried/error/last_checked move. ats, token, confidence and
        # job_count all keep whatever they already had, job_count
        # especially: this run genuinely doesn't know it, and the normal
        # upsert below would write this payload's 0 over the real number.
        # last_checked still advances, because the board really was
        # checked -- freshness and the merge's own last_checked-based
        # conflict resolution both depend on that being honest.
        if r.get("unchanged"):
            conn.execute(
                """
                INSERT INTO companies (domain, ats, token, confidence, job_count, tried, error, first_seen, last_checked)
                VALUES (?, ?, ?, 'verified', ?, ?, NULL, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    tried = excluded.tried,
                    error = NULL,
                    last_checked = excluded.last_checked
                """,
                (domain, ats, token, r.get("job_count", 0), r.get("tried", 0), ts, ts),
            )
            continue

        # Reported live, repeatedly, and NOT fixed by removing the loser
        # domain from domains.txt alone (Cato: cato.networks and
        # catonetworks.com both resolved to greenhouse:catonetworks --
        # the exact same board, fetched and stored twice under two
        # different company_domain values, so every job on it appeared
        # "duplicated"). That's a company-identity collision, not a job
        # upsert bug -- job_id() is keyed on (domain, ats, external_id),
        # so two domains sharing one real board were always going to
        # produce two distinct ids for what's the same posting. The
        # general, not-hardcoded-to-Cato fix: if some OTHER domain
        # already owns this exact (ats, token) pair, this domain is an
        # alias of it, not a second real company -- demote it to a
        # miss-like state and close out whatever jobs it previously
        # accumulated under its own name, rather than upserting a
        # second copy of the same board every single run. Whichever
        # domain the DB already recognizes as the resolved owner wins
        # (stability across runs); if neither is resolved yet, the
        # alphabetically-first domain wins -- arbitrary, but
        # deterministic, so which one "wins" doesn't flap run to run.
        if ats and token:
            other = conn.execute(
                "SELECT domain FROM companies WHERE ats = ? AND token = ? AND domain != ?",
                (ats, token, domain),
            ).fetchone()
            if other:
                canonical_already_resolved = bool(
                    conn.execute(
                        "SELECT 1 FROM companies WHERE domain = ? AND ats IS NOT NULL", (other["domain"],)
                    ).fetchone()
                )
                if canonical_already_resolved or other["domain"] < domain:
                    _demote_alias(conn, domain, other["domain"], ats, token, ts)
                    continue
                # This run is the first time both domains show up together
                # and this one alphabetically precedes the other -- this
                # domain stays canonical instead, so demote the other one.
                _demote_alias(conn, other["domain"], domain, ats, token, ts)

        # Snapshot BEFORE this run's own companies upsert below overwrites
        # it -- a company already resolved to COMEET on a prior run vs.
        # one landing on comeet for the very first time this run needs
        # different treatment for Comeet's unreliable time_updated, see
        # upsert_job's own comment. Specifically ats = 'comeet', not just
        # "ats IS NOT NULL" -- reported live (gloat.com): a domain that
        # had been resolving to something else (or nothing) every prior
        # run and only reached its real Comeet board today read as
        # "already tracked" under the old NOT-NULL check, so its 5
        # genuinely different real time_updated values (spread across
        # three separate days, confirmed against Comeet's own API) all
        # got overwritten with this run's own capture time instead --
        # the exact "brand new company's backlog reads as all 1-minute-
        # old" bug this check exists to prevent, just reached a different
        # way than the original Dream Security case.
        already_tracked = bool(
            conn.execute("SELECT 1 FROM companies WHERE domain = ? AND ats = 'comeet'", (domain,)).fetchone()
        )
        # jsonld is probe.py's own best-effort tier (schema.org JobPosting
        # scraped off the company's careers page, no live API to verify
        # against, see f_embed_scrape's docstring), so it carries the
        # same lower confidence here as the separate --deep scraper output
        # does in load_deep() below. Every other ats value, including the
        # newer workday and personio ones, is a live API response and is
        # 'verified' same as always.
        confidence = None if not ats else ("best_effort" if ats == "jsonld" else "verified")

        # ats=None with retryable=True is an inconclusive result (a
        # --known re-poll of an already-resolved board failing, e.g. a
        # timeout), not the same as a confirmed MISS (--batch discovery
        # genuinely finding no valid ATS, ats=None with retryable=False).
        # The class-level docstring above already promises a MISS leaves
        # this domain "alone entirely," but only the jobs-closing skip
        # below actually did that. This upsert ran unconditionally and
        # wiped a previously-confirmed ats/confidence back to NULL on
        # nothing more than a transient failure. Only tried/error/
        # last_checked update here; ats/token/confidence/job_count keep
        # whatever they already were (NULL if this is a genuinely new,
        # never-resolved domain).
        #
        # Reported live: retryable used to be inferred from error's mere
        # presence, but resolve()'s own genuine "searched everything, no
        # match" result also sets error (to explain the MISS, not to flag
        # it as transient) -- so a real, confident correction (a company
        # wrongly resolved once, correctly returning no match on a later
        # run, e.g. after a false-positive fix) was being silently
        # swallowed here forever instead of ever landing. probe.py now
        # sets this field explicitly instead of leaving it to be inferred.
        inconclusive = ats is None and r.get("retryable")
        if inconclusive:
            conn.execute(
                """
                INSERT INTO companies (domain, ats, token, confidence, job_count, tried, error, first_seen, last_checked)
                VALUES (?, NULL, NULL, NULL, 0, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    tried = excluded.tried,
                    error = excluded.error,
                    last_checked = excluded.last_checked
                """,
                (domain, r.get("tried", 0), r.get("error"), ts, ts),
            )
        else:
            conn.execute(
                """
                INSERT INTO companies (domain, ats, token, confidence, job_count, tried, error, first_seen, last_checked)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    ats = excluded.ats,
                    token = excluded.token,
                    confidence = excluded.confidence,
                    job_count = excluded.job_count,
                    tried = excluded.tried,
                    error = excluded.error,
                    last_checked = excluded.last_checked
                """,
                (domain, ats, r.get("token"), confidence,
                 r.get("job_count", 0), r.get("tried", 0), r.get("error"), ts, ts),
            )

        if not ats:
            continue  # MISS (or inconclusive) this run: don't touch this domain's existing jobs

        ids = set()
        for j in r.get("jobs") or []:
            jid = job_id(domain, j.get("ats") or ats, j.get("external_id"), j.get("url"), j.get("title") or "")
            ids.add(jid)
            job_confidence = "best_effort" if j.get("ats") == "jsonld" else "verified"
            # Compared against the stored hash before the upsert
            # overwrites it. An empty description means "this poll didn't
            # fetch one" rather than "there isn't one" (see upsert_job's
            # own CASE on description), so it never queues an upload and
            # never clears an existing blob.
            desc = j.get("description")
            prior_desc = None
            reindex = False
            if desc:
                sha = description_sha(desc)
                row = conn.execute(
                    "SELECT description_sha, description FROM jobs WHERE id = ?", (jid,)
                ).fetchone()
                # Captured before the upsert below overwrites it: the FTS
                # delete needs the exact text that was indexed.
                prior_desc = row["description"] if row else None
                if row is None or row["description_sha"] != sha:
                    changed_descriptions.append((jid, desc))
                    reindex = True
                j["description_sha"] = sha
            upsert_job(conn, jid, domain, j, confidence=job_confidence, ts=ts, already_tracked_company=already_tracked)
            if reindex:
                index_description(conn, jid, desc, prior_desc, rowid_delete)
        seen_ids_by_domain[domain] = ids

    close_missing_jobs(conn, seen_ids_by_domain, ts)

    # After the DB work, never during it. This commit still writes the
    # description column as well, so a failure here costs nothing yet.
    if drop_description and changed_descriptions:
        # Order matters: the FTS row and the S3 blob are both written
        # above, from the text, before this removes it from the column.
        conn.executemany("UPDATE jobs SET description = NULL WHERE id = ?",
                         [(jid,) for jid, _ in changed_descriptions])

    if changed_descriptions and DESCRIPTIONS_BUCKET:
        n = put_many(DESCRIPTIONS_BUCKET, changed_descriptions)
        print(f"uploaded {n}/{len(changed_descriptions)} changed descriptions", file=sys.stderr)

    return {r["domain"] for r in data}


def upsert_job(conn: sqlite3.Connection, jid: str, domain: str, j: dict, confidence: str, ts: str, already_tracked_company: bool = True) -> None:
    conn.execute(
        """
        INSERT INTO jobs (id, company_domain, ats, external_id, title, location, department,
                           url, posted_at, description_chars, description, description_sha, seniority, workplace_type,
                           skills, salary_text, salary_is_estimate, salary_source,
                           confidence, first_seen, last_seen, closed_at, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            location = excluded.location,
            department = excluded.department,
            url = excluded.url,
            -- Workday's posted_at is a relative-string approximation
            -- (_parse_workday_posted_on: "Posted Yesterday" -> now() minus
            -- 1 day), recomputed fresh from that same string on every
            -- poll -- so a job whose real postedOn text never advances
            -- past "Yesterday" gets a *different* now-minus-1-day value
            -- each time, perpetually creeping forward to stay ~1 day old
            -- forever, regardless of how long it's actually been open.
            -- Reported live: a job sat at the very top of the age sort on
            -- every visit. Frozen at whatever it first resolved to on
            -- initial discovery instead -- ages normally from there, like
            -- every ats with a genuine absolute date already does.
            -- "posted_at IS NOT NULL" matters: a one-time cleanup nulled
            -- every existing workday posted_at (the already-stored values
            -- were themselves inflated by the same bug, no honest baseline
            -- to freeze at), and without this guard the CASE below would
            -- freeze them at that NULL forever instead of ever accepting
            -- this run's first real computed value.
            --
            -- Comeet has the identical failure mode, different cause:
            -- time_updated (see _comeet_job's docstring) is the only
            -- timestamp its API exposes, and it moves on any touch to the
            -- listing, not just a real edit -- reported live, listings
            -- first_seen days ago were reading "15m ago" because
            -- something on Comeet's side keeps bumping time_updated on
            -- basically every poll. Same fix: freeze at whatever posted_at
            -- already resolved to, age normally from there.
            --
            -- Unlike workday's synthetic now-minus-N value, this alone
            -- isn't enough for Comeet: freezing only protects a row
            -- *after* it already has a posted_at. Confirmed live -- a
            -- long-standing Comeet listing's time_updated was freshly
            -- bumped again well after this freeze shipped, so a row
            -- captured for the first time right then would still freeze
            -- at an already-inflated "just now" forever, since Comeet
            -- exposes no real creation-date field to fall back on. See the
            -- ats == 'comeet' substitution below, in the VALUES tuple:
            -- the first-ever insert for a Comeet job uses this run's own
            -- timestamp instead of time_updated, which this CASE then
            -- correctly holds forever after, same as any other ats.
            posted_at = CASE WHEN excluded.ats IN ('workday', 'comeet') AND posted_at IS NOT NULL THEN posted_at ELSE excluded.posted_at END,
            -- Keep the existing description/description_chars when this
            -- upsert's own value is empty, rather than blindly overwriting.
            -- Real for two cases today: Comeet and Workday's fast-poll
            -- re-verify never has a description at all (see probe.py's
            -- FETCH_FULL_DESCRIPTIONS), so without this, a description
            -- scrape-discover.yml worked to capture would get nulled back
            -- out on the very next 5-min fast-poll. Every other ats
            -- always sends a real value here, so this is a no-op for them.
            description_chars = CASE WHEN excluded.description_chars > 0 THEN excluded.description_chars ELSE description_chars END,
            description = CASE WHEN excluded.description IS NOT NULL AND excluded.description != '' THEN excluded.description ELSE description END,
            -- Moves with description, never independently: a poll that
            -- carries no description must leave both alone or the hash
            -- would claim a blob that was never written.
            description_sha = CASE WHEN excluded.description IS NOT NULL AND excluded.description != '' THEN excluded.description_sha ELSE description_sha END,
            -- Same "don't null out what a fuller pass already captured"
            -- reasoning as description above -- skills is derived from
            -- it (keyword match), so it goes empty on the exact same
            -- re-verify passes description does.
            skills = CASE WHEN excluded.skills IS NOT NULL AND excluded.skills != '' THEN excluded.skills ELSE skills END,
            -- salary_text needed a stricter guard than "not empty":
            -- reported live -- a Comeet job's discover-pass estimate
            -- (title+description, e.g. a specific "Go Developer" figure)
            -- was getting silently downgraded by the very next fast-poll,
            -- because Comeet's fast-poll has no description at all, so
            -- probe.py's estimate falls back to matching the bare title
            -- alone -- still a real, non-empty value (the generic
            -- "backend" catch-all), so the old "not empty" guard let it
            -- overwrite the better one every 5 minutes. A new estimate
            -- now only wins when it's a real disclosed salary (never an
            -- estimate, always the most trustworthy), or this pass had
            -- real description text to estimate from (as good or better
            -- signal than whatever's already stored), or nothing was
            -- stored yet at all (something beats nothing on a first
            -- pass). Otherwise the existing, better-informed value stands.
            --
            -- The fourth branch is the one that lets the estimator get
            -- QUIETER. probe.py now declines to estimate where it would
            -- have produced something useless, but every branch above
            -- requires a new non-empty value to overwrite with, so
            -- without this a row that already carries a bad estimate
            -- keeps it for the life of the listing. A pass carrying real
            -- description text is fully informed by definition, so if
            -- THAT pass produced nothing, the stored estimate is
            -- withdrawn. Only ever an estimate: a disclosed salary is
            -- never cleared by anything.
            salary_text = CASE
                WHEN excluded.salary_text IS NOT NULL AND excluded.salary_text != '' AND excluded.salary_is_estimate = 0 THEN excluded.salary_text
                WHEN excluded.salary_text IS NOT NULL AND excluded.salary_text != '' AND excluded.description IS NOT NULL AND excluded.description != '' THEN excluded.salary_text
                -- The description guard above exists because the Israeli
                -- table reads the listing's body, so a pass without one
                -- produces a worse estimate from the bare title. The
                -- learned model never looks at the body at all: it keys
                -- on company, market, level and department. So its
                -- estimate is never the degraded version of itself, and
                -- holding it behind a guard written for a different
                -- estimator only pins stale figures in place on the
                -- ATSes whose fast poll carries no description.
                -- Refreshing freely also means the daily rebuild of the
                -- cells reaches listings within one poll instead of
                -- whenever their body next happens to be fetched.
                WHEN excluded.salary_source = 'estimated' AND salary_is_estimate = 1 THEN excluded.salary_text
                WHEN salary_text IS NULL AND excluded.salary_text IS NOT NULL AND excluded.salary_text != '' THEN excluded.salary_text
                WHEN (excluded.salary_text IS NULL OR excluded.salary_text = '')
                     AND excluded.description IS NOT NULL AND excluded.description != ''
                     AND salary_is_estimate = 1 THEN NULL
                ELSE salary_text
            END,
            salary_is_estimate = CASE
                WHEN excluded.salary_text IS NOT NULL AND excluded.salary_text != '' AND excluded.salary_is_estimate = 0 THEN excluded.salary_is_estimate
                WHEN excluded.salary_text IS NOT NULL AND excluded.salary_text != '' AND excluded.description IS NOT NULL AND excluded.description != '' THEN excluded.salary_is_estimate
                -- The description guard above exists because the Israeli
                -- table reads the listing's body, so a pass without one
                -- produces a worse estimate from the bare title. The
                -- learned model never looks at the body at all: it keys
                -- on company, market, level and department. So its
                -- estimate is never the degraded version of itself, and
                -- holding it behind a guard written for a different
                -- estimator only pins stale figures in place on the
                -- ATSes whose fast poll carries no description.
                -- Refreshing freely also means the daily rebuild of the
                -- cells reaches listings within one poll instead of
                -- whenever their body next happens to be fetched.
                WHEN excluded.salary_source = 'estimated' AND salary_is_estimate = 1 THEN excluded.salary_is_estimate
                WHEN salary_text IS NULL AND excluded.salary_text IS NOT NULL AND excluded.salary_text != '' THEN excluded.salary_is_estimate
                WHEN (excluded.salary_text IS NULL OR excluded.salary_text = '')
                     AND excluded.description IS NOT NULL AND excluded.description != ''
                     AND salary_is_estimate = 1 THEN 0
                ELSE salary_is_estimate
            END,
            -- Branch for branch identical to salary_text above, because
            -- the two must never disagree. A row claiming a disclosed
            -- salary whose text is actually an estimate is worse than
            -- either alone: the UI would present our guess as the
            -- employer's own figure.
            salary_source = CASE
                WHEN excluded.salary_text IS NOT NULL AND excluded.salary_text != '' AND excluded.salary_is_estimate = 0 THEN excluded.salary_source
                WHEN excluded.salary_text IS NOT NULL AND excluded.salary_text != '' AND excluded.description IS NOT NULL AND excluded.description != '' THEN excluded.salary_source
                -- The description guard above exists because the Israeli
                -- table reads the listing's body, so a pass without one
                -- produces a worse estimate from the bare title. The
                -- learned model never looks at the body at all: it keys
                -- on company, market, level and department. So its
                -- estimate is never the degraded version of itself, and
                -- holding it behind a guard written for a different
                -- estimator only pins stale figures in place on the
                -- ATSes whose fast poll carries no description.
                -- Refreshing freely also means the daily rebuild of the
                -- cells reaches listings within one poll instead of
                -- whenever their body next happens to be fetched.
                WHEN excluded.salary_source = 'estimated' AND salary_is_estimate = 1 THEN excluded.salary_source
                WHEN salary_text IS NULL AND excluded.salary_text IS NOT NULL AND excluded.salary_text != '' THEN excluded.salary_source
                WHEN (excluded.salary_text IS NULL OR excluded.salary_text = '')
                     AND excluded.description IS NOT NULL AND excluded.description != ''
                     AND salary_is_estimate = 1 THEN NULL
                ELSE salary_source
            END,
            seniority = excluded.seniority,
            workplace_type = excluded.workplace_type,
            last_seen = excluded.last_seen,
            closed_at = NULL,
            -- raw_json is no longer written; see the NULL bound for it
            -- below. Left in the statement so the column keeps existing
            -- for partitions that still carry values from before.
            raw_json = NULL
        """,
        (jid, domain, j.get("ats"), j.get("external_id"), j.get("title") or "",
         j.get("location"), j.get("department"), j.get("url"),
         # Comeet's own time_updated is not a creation date (see the CASE
         # above) -- on a genuine first insert (posted_at IS NULL, so the
         # CASE takes this branch) use this run's own timestamp instead,
         # not the ATS's unreliable one.
         #
         # Only when the *company* was already tracked before this run,
         # though. Reported live: onboarding Dream Security (30 jobs, all
         # first-ever-inserted in the same run) showed every single one as
         # "1 minute ago" -- true of a job resurfacing on a company we'd
         # already been polling for weeks (time_updated bumped by
         # incidental Comeet-side touches right before we happened to
         # (re-)capture it), but wrong for a brand-new company's entire
         # backlog: flattening 30 genuinely different real ages into one
         # identical synthetic timestamp is a worse, more visibly fake
         # signal than trusting Comeet's real (if individually imperfect)
         # values would have been. A new company's jobs keep their real
         # reported time_updated, same as every other ats.
         ts if j.get("ats") == "comeet" and already_tracked_company else j.get("posted_at"),
         j.get("description_chars", 0), j.get("description"), j.get("description_sha"),
         j.get("seniority"), j.get("workplace_type"),
         ",".join(j.get("skills") or []), j.get("salary_text"), int(bool(j.get("salary_is_estimate"))),
         # Derived rather than required, so a probe.py that predates
         # salary_source still loads: an old payload carrying only
         # salary_is_estimate lands on "table", which is what every
         # estimate was before the learned model existed.
         (j.get("salary_source")
          or (None if not j.get("salary_text")
              else "table" if j.get("salary_is_estimate") else "disclosed")),
         confidence, ts, ts,
         # raw_json: NULL, not the record. Measured on the live
         # snapshot: 583MB across 134,713 rows, 48% of the whole file
         # and larger than every description put together. Nothing reads
         # it anywhere in the codebase, every field inside it except
         # token is already a typed column on the same row, and 70% of
         # rows carried a second full copy of the description in it. The
         # schema calls it "for reprocessing without a re-scrape", but a
         # duplicate of data we already hold is not worth half the file
         # that every reader has to download inside a request.
         None),
    )


def _demote_alias(conn: sqlite3.Connection, domain: str, canonical_domain: str, ats: str, token: str, ts: str) -> None:
    """`domain` resolves to the exact same (ats, token) board as
    `canonical_domain` -- not a second real company, just a second name
    for the same one (a legacy domain, an alternate TLD, ...). Close out
    whatever jobs it accumulated under its own company_domain (the same
    listings now live under canonical_domain instead) and demote its
    companies row to a miss, same shape as a domain that never resolved
    at all, so it stops being treated as resolved (known.json export,
    the fast-poll's --known list, /api/companies) and stops re-fetching
    a board someone else already owns.
    """
    conn.execute(
        "UPDATE jobs SET closed_at = ? WHERE company_domain = ? AND closed_at IS NULL", (ts, domain)
    )
    conn.execute(
        """
        INSERT INTO companies (domain, ats, token, confidence, job_count, tried, error, first_seen, last_checked)
        VALUES (?, NULL, NULL, NULL, 0, 0, ?, ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            ats = NULL, token = NULL, confidence = NULL, job_count = 0,
            error = excluded.error, last_checked = excluded.last_checked
        """,
        (domain, f"alias of {canonical_domain} (both resolve to {ats}:{token})", ts, ts),
    )


def prune_stale_companies(conn: sqlite3.Connection, current_domains: set[str], ts: str) -> int:
    """A `--batch domains.txt` run's resolved.json has one entry per
    domain CURRENTLY in domains.txt/companies.yml -- the full universe
    this project means to track, hit or miss, this run (see main()'s
    --prune-stale flag, set only for that full-batch invocation, never
    for --known's own partial re-poll). A company still marked resolved
    here whose domain ISN'T in that set is a leftover from a domain that
    used to be tracked and no longer is: reported live (Cato, again) --
    removing a domain from domains.txt alone never stopped it being
    re-fetched, because nothing ever told the fast-poll's own known.json
    export (companies WHERE ats IS NOT NULL, every load) to drop it.
    Same close-and-demote treatment as _demote_alias, for the same
    reason: stop it being treated as resolved anywhere downstream
    without losing the historical job rows.
    """
    stale = [
        row["domain"] for row in conn.execute("SELECT domain FROM companies WHERE ats IS NOT NULL")
        if row["domain"] not in current_domains
    ]
    for domain in stale:
        conn.execute(
            "UPDATE jobs SET closed_at = ? WHERE company_domain = ? AND closed_at IS NULL", (ts, domain)
        )
        conn.execute(
            """
            UPDATE companies SET ats = NULL, token = NULL, confidence = NULL, job_count = 0,
                error = ?, last_checked = ?
            WHERE domain = ?
            """,
            (f"no longer in domains.txt/companies.yml as of {ts}", ts, domain),
        )
    return len(stale)


def close_missing_jobs(conn: sqlite3.Connection, seen_ids_by_domain: dict[str, set[str]], ts: str) -> None:
    """A job still open in the DB, for a domain we successfully re-probed
    this run, that didn't come back in this run's results: mark it closed.
    """
    for domain, seen_ids in seen_ids_by_domain.items():
        rows = conn.execute(
            "SELECT id FROM jobs WHERE company_domain = ? AND closed_at IS NULL", (domain,)
        ).fetchall()
        missing = [row[0] for row in rows if row[0] not in seen_ids]
        if missing:
            conn.executemany(
                "UPDATE jobs SET closed_at = ? WHERE id = ?", [(ts, jid) for jid in missing]
            )


def load_deep(conn: sqlite3.Connection, deep_path: Path) -> None:
    """Layer in the best-effort scraper's output:
        [{"domain": ..., "jobs": [{"title":..., "location":..., "url":..., "department":...}, ...]}, ...]
    No posted_at, no company-level upsert (finding job listings doesn't
    verify a real ATS), no close-missing-jobs pass (infrequent and
    best-effort, so a job not reappearing isn't strong evidence of closure).
    """
    if not deep_path.exists():
        return
    data = json.loads(deep_path.read_text(encoding="utf-8"))
    ts = now_iso()
    for r in data:
        domain = r["domain"]
        for j in r.get("jobs") or []:
            jid = job_id(domain, "best_effort", None, j.get("url"), j.get("title") or "")
            upsert_job(conn, jid, domain, dict(j, ats="best_effort"), confidence="best_effort", ts=ts)


def check_timestamp_clustering(conn: sqlite3.Connection) -> list[str]:
    """Detects the exact failure signature both of today's real incidents
    shared (gloat.com's Comeet timestamps stamped with the run's own
    capture time; Workday's "Posted Today" bucket stamped with the exact
    instant it happened to be checked): many jobs, across DIFFERENT
    companies, all sharing the identical posted_at value -- the tell
    that something got a shared "now" instead of each job's own real
    date. One company legitimately batch-posting several roles at once
    can share a timestamp; many DIFFERENT companies sharing the exact
    same one, to the microsecond, essentially never happens by real
    coincidence.

    Doesn't fix anything by itself -- both incidents were permanent
    (not self-correcting) because of the freeze-on-update logic in
    upsert_job's own posted_at CASE, which exists on purpose to protect
    a good value from being degraded by a worse later pass, and that
    same protection means ANY future one-time computation bug, for any
    ATS, gets frozen in exactly the same way. This is the safety net
    for that next time: noticed within a day (every load calls this),
    not sitting silently until someone happens to see it on the board.
    """
    rows = conn.execute(
        """
        SELECT posted_at, COUNT(*) AS n, COUNT(DISTINCT company_domain) AS companies
        FROM jobs
        WHERE closed_at IS NULL AND posted_at IS NOT NULL
          -- Exact midnight is excluded, not suspicious: Workday's own
          -- day-precision dates (probe.py's _workday_job_detail, the
          -- real startDate field) are only ever known to the day, not
          -- the instant, so every company that genuinely posted
          -- something on the same real calendar day correctly shares
          -- this same value -- confirmed live, immediately after
          -- today's own Workday fix: 44 jobs across 11 real companies
          -- all legitimately posted today. A real coincidence needs
          -- second/microsecond precision to mean anything.
          AND posted_at NOT LIKE '%T00:00:00%'
        GROUP BY posted_at
        HAVING n >= 5 AND companies >= 2
        ORDER BY n DESC
        """
    ).fetchall()
    return [
        f"{r['posted_at']}: {r['n']} jobs across {r['companies']} different companies share this exact timestamp"
        for r in rows
    ]


def update_meta(conn: sqlite3.Connection) -> None:
    counts = conn.execute(
        "SELECT confidence, COUNT(*) FROM jobs WHERE closed_at IS NULL GROUP BY confidence"
    ).fetchall()
    total_companies = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    hit_companies = conn.execute("SELECT COUNT(*) FROM companies WHERE ats IS NOT NULL").fetchone()[0]
    clustering_warnings = check_timestamp_clustering(conn)
    if clustering_warnings:
        print("TIMESTAMP CLUSTERING WARNING (see check_timestamp_clustering's own docstring):", file=sys.stderr)
        for w in clustering_warnings:
            print(f"  {w}", file=sys.stderr)
    meta = {
        "last_loaded": now_iso(),
        "open_jobs_verified": next((n for c, n in counts if c == "verified"), 0),
        "open_jobs_best_effort": next((n for c, n in counts if c == "best_effort"), 0),
        "companies_total": total_companies,
        "companies_resolved": hit_companies,
        # Empty string, not "0" or omitted: route_health() (api/handler.py)
        # reads this directly and a falsy-but-present value is easier to
        # branch on there than distinguishing "never checked" from "checked,
        # clean."
        "timestamp_clustering_warnings": "; ".join(clustering_warnings),
    }
    for k, v in meta.items():
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, str(v)),
        )


def s3_pull(bucket: str, key: str, dest: Path) -> tuple[bool, str | None]:
    """Returns (existed, ETag). The ETag is this function's real point --
    see s3_push_conditional's own docstring for why."""
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3")
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        dest.write_bytes(resp["Body"].read())
        return True, resp["ETag"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        # AccessDenied counts as "not there" when the caller can't list
        # the bucket. S3 deliberately hides the difference between a
        # missing object and one you may not read unless you hold
        # s3:ListBucket, so a first write to a brand-new key comes back
        # 403, not 404. These roles are scoped to object ARNs on purpose
        # and have no ListBucket, so every partition's very first write
        # hit this and died.
        #
        # Confirmed live (2026-09-09): the shard count had grown to 70
        # while only partitions 0-39 existed, so every shard above 39
        # scraped its companies, reported the job count, then threw the
        # results away when the loader crashed. Roughly 43% of shards had
        # been failing to persist anything for at least six hours, and
        # nothing above 39 could ever be created because creating it was
        # the thing that failed.
        if code in ("404", "NoSuchKey", "AccessDenied", "403"):
            return False, None
        raise


def s3_push(bucket: str, key: str, src: Path) -> None:
    import boto3

    boto3.client("s3").upload_file(str(src), bucket, key)


def s3_push_conditional(bucket: str, key: str, src: Path, etag: str | None) -> bool:
    """Same upload as s3_push, except it fails cleanly (returns False)
    instead of overwriting anything if `key` has changed in S3 since the
    ETag this call was given was pulled -- If-Match (or If-None-Match,
    when etag is None, meaning the object didn't exist yet) turns S3's
    default "whoever PUTs last wins, silently" into a detected conflict.

    THE actual fix for a whole family of bugs this project kept hitting
    (Cato's duplicate reappearing after being fixed by hand, gloat.com's
    real Comeet timestamps reverting back to a fake capture-time value):
    jobs.db is one shared ~100MB file, read-modify-written whole by two
    genuinely-independent writers (the 5-min fast-poll and the once-daily
    discover run, or two overlapping fast-poll invocations if one runs
    long) with no coordination between them at all. Whichever one
    finished uploading LAST always won outright before this existed --
    manually pausing the fast-poll's EventBridge schedule around a
    one-off hand fix (the approach used earlier this same day) reduces
    that window but doesn't close it: re-enabling the schedule can itself
    trigger a new invocation that starts downloading before the paused
    fix's own upload finishes, still racing it. See main()'s retry loop
    for the other half of this: on a conflict, redo the whole attempt
    against a fresh pull, don't just retry the upload with stale data.
    """
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3")
    kwargs: dict = {"Bucket": bucket, "Key": key, "Body": src.read_bytes()}
    if etag is not None:
        kwargs["IfMatch"] = etag
    else:
        kwargs["IfNoneMatch"] = "*"
    try:
        s3.put_object(**kwargs)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("PreconditionFailed", "412"):
            return False
        raise


def export_known(conn: sqlite3.Connection, path: Path) -> int:
    """Write every resolved company's (domain, ats, token) as JSON, in the
    shape probe.py's --known expects. Written on every load so a newly
    discovered company is available to the next fast poll immediately.
    """
    rows = conn.execute("SELECT domain, ats, token FROM companies WHERE ats IS NOT NULL").fetchall()
    known = [{"domain": r["domain"], "ats": r["ats"], "token": r["token"]} for r in rows]
    path.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
    return len(known)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolved", required=True, type=Path,
                     help="probe.py --json output (either --batch's full discovery run or --known's fast-poll run, same shape, loaded the same way)")
    ap.add_argument("--deep", type=Path, help="best-effort scraper output (optional)")
    ap.add_argument("--out", required=True, type=Path, help="local jobs.db path")
    ap.add_argument("--known-out", type=Path, default=None,
                     help="local path to write known.json (default: alongside --out, named known.json)")
    ap.add_argument("--bucket", help="S3 bucket to pull the existing DB from / push the result to")
    ap.add_argument("--key", default="jobs.db", help="S3 key for jobs.db (default: jobs.db)")
    ap.add_argument("--known-key", default="known.json", help="S3 key for known.json (default: known.json)")
    ap.add_argument("--prune-stale", action="store_true",
                     help="demote any resolved company whose domain isn't in this run's --resolved data "
                          "(see prune_stale_companies) -- only correct for a full --batch domains.txt run, "
                          "never for --known's own partial re-poll, so scrape-fast.yml must never pass this")
    ap.add_argument("--skip-vacuum", action="store_true",
                     help="scrape_handler.py's sharded re-poll passes this: VACUUM rewrites the WHOLE DB file "
                          "regardless of how few rows this run touched, so paying that cost on every ~5-minute "
                          "shard cycle scales with total jobs.db size, not shard size -- the exact growth this "
                          "sharding exists to avoid. scrape_maintenance_handler.py's own daily, --resolved-less "
                          "run is where VACUUM actually happens now; every other caller (scrape-discover.yml's "
                          "full batch, merge_discovered_batch.py) keeps vacuuming every run, unchanged.")
    ap.add_argument("--drop-description", action="store_true",
                     help="index the description and upload its blob, but store NULL in the "
                          "description column. The applier writes straight into jobs-read.db, "
                          "which must stay small: description text was ~42% of that file and the "
                          "words remain searchable through jobs_fts either way.")
    ap.add_argument("--skip-known", action="store_true",
                     help="a --key pointed at a partition file (jobs-partition-{name}.db, see the Partition & "
                          "Merge design doc) only ever holds ITS OWN shard's companies -- export_known() run "
                          "against a partition would produce a known.json missing every company outside that "
                          "one shard, and pushing it would clobber the real, global known.json that "
                          "_pick_shard() and the fast-poll's own re-check depend on. Partition-writing callers "
                          "pass this; known.json stays the merge step's job instead, once it exists, since "
                          "that's the only place with a full, current view of every company again.")
    args = ap.parse_args()

    # See s3_push_conditional's own docstring for why this is a retry
    # loop and not a single pull-modify-push. Each attempt re-pulls from
    # scratch (no "reuse the local file if it already exists" shortcut
    # the single-shot version used to have) specifically so the ETag it
    # conditions its push on is never stale -- a warm Lambda container
    # reusing a leftover /tmp/jobs.db from a previous invocation would
    # otherwise condition on an ETag from before ITS OWN last write,
    # guaranteeing every push after the first one fails the check.
    attempts = 5
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            print(f"jobs.db changed in S3 since it was pulled -- retrying (attempt {attempt}/{attempts})",
                  file=sys.stderr)

        etag = None
        if args.bucket:
            if args.out.exists():
                args.out.unlink()
            existed, etag = s3_pull(args.bucket, args.key, args.out)
            print(f"pulled existing jobs.db from s3://{args.bucket}/{args.key}: {existed}", file=sys.stderr)

        conn = open_db(args.out)
        with conn:
            current_domains = load_resolved(conn, args.resolved, args.drop_description)
            if args.deep:
                load_deep(conn, args.deep)
            if args.prune_stale:
                ts = now_iso()
                n_pruned = prune_stale_companies(conn, current_domains, ts)
                if n_pruned:
                    print(f"pruned {n_pruned} companies no longer in domains.txt/companies.yml", file=sys.stderr)
            update_meta(conn)

        known_out = args.known_out or args.out.with_name("known.json")
        n_known = None if args.skip_known else export_known(conn, known_out)
        if not args.skip_vacuum:
            conn.execute("VACUUM")
        conn.close()

        print(f"wrote {args.out} ({args.out.stat().st_size} bytes)", file=sys.stderr)

        if not args.bucket:
            return 0

        if not s3_push_conditional(args.bucket, args.key, args.out, etag):
            continue  # someone else won this round -- redo the whole attempt against a fresh pull
        print(f"pushed jobs.db to s3://{args.bucket}/{args.key}", file=sys.stderr)

        if args.skip_known:
            return 0

        # known.json has no reader that needs it pinned to one exact
        # jobs.db version (the fast-poll just wants "the latest resolved
        # companies," not a specific snapshot), so a plain overwrite here
        # is fine even though jobs.db's own push just went through the
        # conditional path -- no need to re-run the whole cycle over a
        # conflict on this file alone.
        s3_push(args.bucket, args.known_key, known_out)
        print(f"pushed known.json ({n_known} companies) to s3://{args.bucket}/{args.known_key}", file=sys.stderr)
        return 0

    print(f"gave up after {attempts} attempts, jobs.db kept changing underneath us", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
