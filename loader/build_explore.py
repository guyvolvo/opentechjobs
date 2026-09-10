"""The database the Explore page queries in the browser.

The snapshot the API reads is 308MB and built for one job: answering the
board's own queries fast. Two thirds of it is a search index over
descriptions, and the rest is shaped for the loader's upsert path. None
of that is what someone typing SQL wants.

This is a second, slimmer database built from it: every verified
listing, the columns worth grouping and filtering on, one derived table
that makes the common questions one-liners, and the indexes those
questions need. No descriptions, no search index. Measured on the live
snapshot: 142,000 listings in about 78MB, built in a few seconds.

It is served as a static file and read by SQLite compiled to
WebAssembly, which fetches only the 4KB pages a query touches by HTTP
range request. So the size matters far less than the shape: an indexed
query reads the same few pages whether the file is 70MB or 700, and a
visitor never downloads the whole thing. What size does cost is the
upload, which is why this is paced to once an hour rather than run on
every apply.

Rebuilt from scratch each time, never upserted. That keeps the file
compact and the page cache honest: an hourly rebuild means CloudFront
can hold every page of the previous build for the full hour, which is
what makes repeat queries free.
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Two layouts to satisfy, same as precompute.py: api/ in a checkout,
# flattened beside the handler in the deployed package.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "api"))

from job_filters import classify_category  # noqa: E402

KEY = "explore.db"

# Once an hour. A rebuild is cheap; the upload is 70MB, and every rebuild
# invalidates every cached page at the edge. Fifteen-minute freshness on
# a file people run ad-hoc analysis against buys nothing a visitor would
# notice and costs a cold cache four times an hour.
MAX_AGE_S = 3600

# Matches the runtime's requestChunkSize. A query touching an index
# reads a handful of these; a full scan reads all of them, which is slow
# for that one visitor and costs nothing for anyone else.
PAGE_SIZE = 4096

SCHEMA = """
CREATE TABLE jobs (
  id            TEXT PRIMARY KEY,
  company       TEXT NOT NULL,   -- companies.domain
  ats           TEXT NOT NULL,
  title         TEXT NOT NULL,
  category      TEXT,            -- normalised from department and title, same function the board filters on
  department    TEXT,            -- the employer's own label, unnormalised
  seniority     TEXT,            -- intern|junior|mid|senior|staff|principal|lead|manager|director|exec|NULL
  workplace     TEXT,            -- remote|hybrid|onsite|NULL
  location      TEXT,            -- raw, as the employer wrote it
  salary_text   TEXT,            -- as shown on the board
  salary_source TEXT,            -- disclosed|table|estimated|NULL
  url           TEXT,
  posted_at     TEXT,            -- the employer's own date, when the ATS reports one
  first_seen    TEXT NOT NULL,   -- when this board first saw it
  last_seen     TEXT NOT NULL,
  closed_at     TEXT,            -- NULL while open
  days_open     REAL             -- closed_at - first_seen for closed rows, now - first_seen for open ones
);

-- skills is comma-joined text on jobs, up to five terms. Split out so
-- "which skills appear most in senior roles" is a GROUP BY rather than
-- string surgery.
CREATE TABLE job_skills (
  job_id TEXT NOT NULL,
  skill  TEXT NOT NULL
);

CREATE TABLE companies (
  domain     TEXT PRIMARY KEY,
  name       TEXT,               -- as the ATS reports it, NULL until resolved
  ats        TEXT,
  open_jobs  INTEGER NOT NULL DEFAULT 0
);

-- Distinct values per filterable field over open listings, with
-- counts. The builder's pickers read this, one tiny indexed query
-- each, instead of grouping 111,000 rows per field on every page load.
-- Reported live: the page sat with blank controls for as long as it
-- took seven such scans to pull most of the file through range
-- requests on a cold cache.
CREATE TABLE facets (
  field TEXT NOT NULL,
  value TEXT NOT NULL,
  label TEXT,               -- companies: the display name; else NULL
  n     INTEGER NOT NULL
);

-- One row. What the page shows as "data as of".
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""

INDEXES = [
    # Partial covering indexes over open listings, one per column the
    # builder groups by. "WHERE closed_at IS NULL GROUP BY category" is
    # the shape of nearly every question the page asks, and without
    # these SQLite walks the closed_at index to 111,000 rowids and then
    # fetches each row for its category, which through range requests
    # is the whole table. With them the query is a scan of a small
    # index and nothing else is touched. closed_at rides along in each
    # (always NULL, so nearly free) because the planner only calls an
    # index covering when every column the query names is in it, and
    # the WHERE names closed_at.
    "CREATE INDEX ix_open_category ON jobs(category, closed_at) WHERE closed_at IS NULL",
    "CREATE INDEX ix_open_seniority ON jobs(seniority, closed_at) WHERE closed_at IS NULL",
    "CREATE INDEX ix_open_workplace ON jobs(workplace, closed_at) WHERE closed_at IS NULL",
    "CREATE INDEX ix_open_ats ON jobs(ats, closed_at) WHERE closed_at IS NULL",
    "CREATE INDEX ix_open_salary_source ON jobs(salary_source, closed_at) WHERE closed_at IS NULL",
    "CREATE INDEX ix_open_company ON jobs(company, closed_at) WHERE closed_at IS NULL",
    "CREATE INDEX ix_open_first_seen ON jobs(first_seen, closed_at) WHERE closed_at IS NULL",
    "CREATE INDEX ix_facets_field ON facets(field, n DESC)",
    "CREATE INDEX ix_jobs_company ON jobs(company)",
    "CREATE INDEX ix_jobs_category ON jobs(category)",
    "CREATE INDEX ix_jobs_seniority ON jobs(seniority)",
    "CREATE INDEX ix_jobs_workplace ON jobs(workplace)",
    "CREATE INDEX ix_jobs_ats ON jobs(ats)",
    "CREATE INDEX ix_jobs_closed ON jobs(closed_at)",
    "CREATE INDEX ix_jobs_first_seen ON jobs(first_seen)",
    "CREATE INDEX ix_jobs_salary_source ON jobs(salary_source)",
    "CREATE INDEX ix_skills_skill ON job_skills(skill)",
    "CREATE INDEX ix_skills_job ON job_skills(job_id)",
]


def build(snapshot: Path, out: Path) -> dict:
    """Write the explore database. Returns a summary."""
    if out.exists():
        out.unlink()
    src = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(out)
    dst.execute(f"PRAGMA page_size = {PAGE_SIZE}")
    dst.executescript(SCHEMA)

    now = datetime.now(timezone.utc)
    rows = src.execute(
        """SELECT id, company_domain, ats, title, department, seniority, workplace_type,
                  location, skills, salary_text, salary_source, url, posted_at,
                  first_seen, last_seen, closed_at
           FROM jobs WHERE confidence = 'verified'"""
    ).fetchall()

    job_rows, skill_rows = [], []
    for r in rows:
        end = _parse(r["closed_at"]) or now
        start = _parse(r["first_seen"])
        days_open = round((end - start).total_seconds() / 86400, 2) if start else None
        job_rows.append((
            r["id"], r["company_domain"], r["ats"], r["title"],
            classify_category(r["department"], r["title"]), r["department"],
            r["seniority"], r["workplace_type"], r["location"],
            r["salary_text"], r["salary_source"], r["url"], r["posted_at"],
            r["first_seen"], r["last_seen"], r["closed_at"], days_open,
        ))
        for term in (r["skills"] or "").split(","):
            term = term.strip().lower()
            if term:
                skill_rows.append((r["id"], term))

    dst.executemany("INSERT INTO jobs VALUES (%s)" % ",".join("?" * 17), job_rows)
    dst.executemany("INSERT INTO job_skills VALUES (?, ?)", skill_rows)
    dst.executemany(
        "INSERT INTO companies VALUES (?, ?, ?, 0)",
        [(c["domain"], c["company_name"], c["ats"])
         for c in src.execute("SELECT domain, company_name, ats FROM companies")],
    )
    # Indexes before the open-count update, not after. As a correlated
    # subquery over an unindexed jobs table this was 3,569 companies
    # times a 142,000-row scan, and the whole build measured 90 seconds
    # against under two for everything else. One grouped pass instead.
    for ddl in INDEXES:
        dst.execute(ddl)
    dst.executemany(
        "UPDATE companies SET open_jobs = ? WHERE domain = ?",
        [(n, d) for d, n in dst.execute(
            "SELECT company, COUNT(*) FROM jobs WHERE closed_at IS NULL GROUP BY company")],
    )

    # The pickers' values. Computed once here, where it is one grouped
    # pass over a local file, rather than on every page load.
    for field in ("category", "seniority", "workplace", "ats", "salary_source"):
        dst.execute(
            f"""INSERT INTO facets (field, value, label, n)
                SELECT ?, {field}, NULL, COUNT(*) FROM jobs
                WHERE closed_at IS NULL AND {field} IS NOT NULL GROUP BY {field}""",
            (field,),
        )
    dst.execute(
        """INSERT INTO facets (field, value, label, n)
           SELECT 'skill', s.skill, NULL, COUNT(*) FROM job_skills s
           JOIN jobs j ON j.id = s.job_id WHERE j.closed_at IS NULL GROUP BY s.skill"""
    )
    dst.execute(
        """INSERT INTO facets (field, value, label, n)
           SELECT 'company', domain, COALESCE(name, domain), open_jobs
           FROM companies WHERE open_jobs > 0"""
    )

    open_jobs = sum(1 for r in job_rows if r[15] is None)
    meta = {
        "built_at": now.isoformat(),
        "jobs": str(len(job_rows)),
        "open_jobs": str(open_jobs),
        "companies": str(dst.execute("SELECT COUNT(*) FROM companies").fetchone()[0]),
        "skills": str(len(skill_rows)),
        "corpus_since": "2026-09-01",
    }
    dst.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    dst.commit()
    # Without statistics the planner guesses that "closed_at IS NULL"
    # matches a handful of rows, seeks the plain closed_at index, and
    # then walks the table for every one of the 111,000 it actually
    # matches. With them it knows better and scans the partial covering
    # index instead. The stat table travels inside the file, so the
    # browser's planner sees the same numbers.
    dst.execute("ANALYZE")
    dst.execute("VACUUM")
    dst.close()
    src.close()
    return {**meta, "bytes": out.stat().st_size}


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _fresh_enough(s3, bucket: str) -> bool:
    try:
        head = s3.head_object(Bucket=bucket, Key=KEY)
    except Exception:
        return False
    age = (datetime.now(timezone.utc) - head["LastModified"]).total_seconds()
    if age < MAX_AGE_S:
        print(f"{KEY} is {age:.0f}s old, leaving it", file=sys.stderr)
        return True
    return False


def publish(frontend_bucket: str, snapshot: Path, work_dir: Path) -> dict | None:
    """Build and upload if the published copy is older than MAX_AGE_S.

    Never raises. This runs inside a merge that has already pushed a
    snapshot, and an analysis page is not worth failing that over.
    """
    if not frontend_bucket:
        return None
    try:
        import boto3

        s3 = boto3.client("s3")
        if _fresh_enough(s3, frontend_bucket):
            return None
        out = work_dir / KEY
        summary = build(snapshot, out)
        # application/octet-stream, and it matters: CloudFront compresses
        # compressible types on the fly, and compression and range
        # requests do not mix. A binary type is left alone, so the
        # runtime's byte offsets mean what it thinks they mean.
        s3.upload_file(
            str(out), frontend_bucket, KEY,
            ExtraArgs={"ContentType": "application/octet-stream",
                       "CacheControl": "public, max-age=3600"},
        )
        print(f"published {KEY}: {json.dumps(summary)}", file=sys.stderr)
        return summary
    except Exception as e:
        print(f"couldn't build or publish {KEY} (non-fatal): {e!r}", file=sys.stderr)
        return None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("explore.db"))
    args = ap.parse_args()
    print(json.dumps(build(args.snapshot, args.out), indent=2))
