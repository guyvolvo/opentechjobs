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
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_id(domain: str, ats: str, external_id: str | None, url: str | None, title: str) -> str:
    """Stable id for a job row. Prefers external_id (ATS-assigned) over url
    (can pick up tracking params) over title (last resort).
    """
    key = f"{domain}|{ats}|{external_id or url or title}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def load_resolved(conn: sqlite3.Connection, resolved_path: Path) -> None:
    """Upsert probe.py's --json output. Every company in this file was
    re-checked THIS run, so companies/jobs not mentioned for a given
    domain but present in the DB from a prior run are fair game to close
    (see close_missing_jobs below), but only for domains that actually
    appear here with ats set. A domain that flips to MISS this run is left
    alone entirely: we have no fresh evidence either way, so touching its
    existing jobs would manufacture a false "closed" signal from what
    might just be a transient probe failure.
    """
    data = json.loads(resolved_path.read_text(encoding="utf-8"))
    ts = now_iso()
    seen_ids_by_domain: dict[str, set[str]] = {}

    for r in data:
        domain = r["domain"]
        ats = r.get("ats")
        # jsonld is probe.py's own best-effort tier (schema.org JobPosting
        # scraped off the company's careers page, no live API to verify
        # against, see f_embed_scrape's docstring), so it carries the
        # same lower confidence here as the separate --deep scraper output
        # does in load_deep() below. Every other ats value, including the
        # newer workday and personio ones, is a live API response and is
        # 'verified' same as always.
        confidence = None if not ats else ("best_effort" if ats == "jsonld" else "verified")

        # ats=None with an error attached is an inconclusive result (a
        # --known re-poll of an already-resolved board failing, e.g. a
        # timeout), not the same as a confirmed MISS (--batch discovery
        # genuinely finding no valid ATS, ats=None with no error). The
        # class-level docstring above already promises a MISS leaves this
        # domain "alone entirely," but only the jobs-closing skip below
        # actually did that. This upsert ran unconditionally and wiped a
        # previously-confirmed ats/confidence back to NULL on nothing more
        # than a transient failure. Only tried/error/last_checked update
        # here; ats/token/confidence/job_count keep whatever they already
        # were (NULL if this is a genuinely new, never-resolved domain).
        inconclusive = ats is None and r.get("error")
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
            upsert_job(conn, jid, domain, j, confidence=job_confidence, ts=ts)
        seen_ids_by_domain[domain] = ids

    close_missing_jobs(conn, seen_ids_by_domain, ts)


def upsert_job(conn: sqlite3.Connection, jid: str, domain: str, j: dict, confidence: str, ts: str) -> None:
    conn.execute(
        """
        INSERT INTO jobs (id, company_domain, ats, external_id, title, location, department,
                           url, posted_at, description_chars, description, seniority, workplace_type,
                           confidence, first_seen, last_seen, closed_at, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
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
            posted_at = CASE WHEN excluded.ats = 'workday' THEN posted_at ELSE excluded.posted_at END,
            -- Keep the existing description/description_chars when this
            -- upsert's own value is empty, rather than blindly overwriting.
            -- Real for two cases today: Comeet and Workday's fast-poll
            -- re-verify never has a description at all (see probe.py's
            -- FETCH_FULL_DESCRIPTIONS), so without this, a description
            -- scrape-discover.yml worked to capture would get nulled back
            -- out on the very next 10-min fast-poll. Every other ats
            -- always sends a real value here, so this is a no-op for them.
            description_chars = CASE WHEN excluded.description_chars > 0 THEN excluded.description_chars ELSE description_chars END,
            description = CASE WHEN excluded.description IS NOT NULL AND excluded.description != '' THEN excluded.description ELSE description END,
            seniority = excluded.seniority,
            workplace_type = excluded.workplace_type,
            last_seen = excluded.last_seen,
            closed_at = NULL,
            raw_json = excluded.raw_json
        """,
        (jid, domain, j.get("ats"), j.get("external_id"), j.get("title") or "",
         j.get("location"), j.get("department"), j.get("url"), j.get("posted_at"),
         j.get("description_chars", 0), j.get("description"), j.get("seniority"), j.get("workplace_type"),
         confidence, ts, ts,
         json.dumps(j, ensure_ascii=False)),
    )


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


def update_meta(conn: sqlite3.Connection) -> None:
    counts = conn.execute(
        "SELECT confidence, COUNT(*) FROM jobs WHERE closed_at IS NULL GROUP BY confidence"
    ).fetchall()
    total_companies = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    hit_companies = conn.execute("SELECT COUNT(*) FROM companies WHERE ats IS NOT NULL").fetchone()[0]
    meta = {
        "last_loaded": now_iso(),
        "open_jobs_verified": next((n for c, n in counts if c == "verified"), 0),
        "open_jobs_best_effort": next((n for c, n in counts if c == "best_effort"), 0),
        "companies_total": total_companies,
        "companies_resolved": hit_companies,
    }
    for k, v in meta.items():
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, str(v)),
        )


def s3_pull(bucket: str, key: str, dest: Path) -> bool:
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3")
    try:
        s3.download_file(bucket, key, str(dest))
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def s3_push(bucket: str, key: str, src: Path) -> None:
    import boto3

    boto3.client("s3").upload_file(str(src), bucket, key)


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
    args = ap.parse_args()

    if args.bucket and not args.out.exists():
        pulled = s3_pull(args.bucket, args.key, args.out)
        print(f"pulled existing jobs.db from s3://{args.bucket}/{args.key}: {pulled}", file=sys.stderr)

    conn = open_db(args.out)
    with conn:
        load_resolved(conn, args.resolved)
        if args.deep:
            load_deep(conn, args.deep)
        update_meta(conn)

    known_out = args.known_out or args.out.with_name("known.json")
    n_known = export_known(conn, known_out)
    conn.execute("VACUUM")
    conn.close()

    if args.bucket:
        s3_push(args.bucket, args.key, args.out)
        print(f"pushed jobs.db to s3://{args.bucket}/{args.key}", file=sys.stderr)
        s3_push(args.bucket, args.known_key, known_out)
        print(f"pushed known.json ({n_known} companies) to s3://{args.bucket}/{args.known_key}", file=sys.stderr)

    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
