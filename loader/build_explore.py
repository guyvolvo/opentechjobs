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

import hashlib
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

# Every build is a new object under this prefix, named by build time
# and content, and the page finds the current one through the manifest.
# The file must never change under a URL: CloudFront caches each range
# separately, so after an in-place upload a browser gets some 4 KB pages
# from the old file and some from the new, and SQLite reports "database
# disk image is malformed". Seen live on 2026-09-10, twenty minutes
# after a rebuild. A new key per build means every cached range of a
# given URL came from one file, and a session that opened an older copy
# keeps reading that copy until it reloads.
PREFIX = "explore/"
MANIFEST = "explore.json"
KEEP = 3   # builds left in the bucket: the current one and two hours of open tabs

# Once an hour. A rebuild is cheap; the upload is 100MB, and a visitor
# who is mid-analysis stays on the copy they opened. Fifteen-minute
# freshness on a file people run ad-hoc analysis against buys nothing
# they would notice.
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
-- One row per skill per listing, carrying the listing's filter columns
-- so a question about skills never has to visit the jobs table. That
-- table is 60 MB of titles and URLs spread over 15,000 pages, and a
-- join from 116,000 skill rows touched nearly all of them through 4 KB
-- range requests: thousands of requests, some of which failed, and a
-- failed read surfaces as "database disk image is malformed". Seen live
-- on every starter query about skills. Ten extra megabytes here, read
-- through a few covering indexes, is the cheaper shape.
CREATE TABLE job_skills (
  job_rowid     INTEGER NOT NULL, -- jobs.rowid; every jobs index entry carries it, so the skill filter needs no row
  job_id        TEXT NOT NULL,
  skill         TEXT NOT NULL,
  company       TEXT NOT NULL,
  ats           TEXT NOT NULL,
  category      TEXT,
  seniority     TEXT,
  workplace     TEXT,
  salary_source TEXT,
  first_seen    TEXT NOT NULL,
  closed_at     TEXT,
  days_open     REAL
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

# One wide covering index per table, and almost nothing else.
#
# This file is read by HTTP range request, 4 KB at a time, and the
# planner's cost model has no idea. Given a small index on the filter
# column it seeks it and then fetches each matching row from the
# table, which over HTTP is thousands of random requests into 60 MB of
# titles and URLs. Some fail, and a failed read comes back as "database
# disk image is malformed". Every starter query about skills or pay did
# this live.
#
# So the table is never the plan. Every column the builder can filter
# or group by sits in one index, led by closed_at so the open half is a
# contiguous range, and there is no narrower index to tempt the planner
# into lookups. A question reads that index (about 22 MB, sequential,
# which the runtime coalesces into a few dozen requests) and the runtime
# keeps those pages, so the next question is mostly free. Rows are only
# fetched for the handful a row-mode question finally shows.
INDEXES = [
    "CREATE INDEX ix_jobs_wide ON jobs(closed_at, category, seniority, workplace, ats, salary_source, company, first_seen, days_open, location, title)",
    # Led by skill, so "listings asking for python" seeks to one range
    # of 14,000 entries instead of reading the whole index, and the
    # listing columns come along for grouping.
    "CREATE INDEX ix_skills_wide ON job_skills(skill, closed_at, category, seniority, workplace, ats, salary_source, company, first_seen, days_open, job_rowid)",
    "CREATE INDEX ix_facets_field ON facets(field, n DESC)",
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

    # Timestamps to the second. The snapshot's carry microseconds and an
    # offset, thirteen bytes a row that would sit in the wide index too.
    short = lambda t: t[:19] if t else t

    job_rows, skills_of = [], {}
    for r in rows:
        end = _parse(r["closed_at"]) or now
        start = _parse(r["first_seen"])
        days_open = round((end - start).total_seconds() / 86400, 2) if start else None
        job_rows.append((
            r["id"], r["company_domain"], r["ats"], r["title"],
            classify_category(r["department"], r["title"]), r["department"],
            r["seniority"], r["workplace_type"], r["location"],
            r["salary_text"], r["salary_source"], r["url"], short(r["posted_at"]),
            short(r["first_seen"]), short(r["last_seen"]), short(r["closed_at"]), days_open,
        ))
        terms = {t.strip().lower() for t in (r["skills"] or "").split(",")}
        terms.discard("")
        if terms:
            skills_of[r["id"]] = (terms, job_rows[-1])

    dst.executemany("INSERT INTO jobs VALUES (%s)" % ",".join("?" * 17), job_rows)
    rowid_of = dict(dst.execute("SELECT id, rowid FROM jobs").fetchall())
    skill_rows = [
        (rowid_of[jid], jid, term, jr[1], jr[2], jr[4], jr[6], jr[7], jr[10], jr[13], jr[15], jr[16])
        for jid, (terms, jr) in skills_of.items() for term in sorted(terms)
    ]
    dst.executemany("INSERT INTO job_skills VALUES (%s)" % ",".join("?" * 12), skill_rows)
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
           SELECT 'skill', skill, NULL, COUNT(*) FROM job_skills
           WHERE closed_at IS NULL GROUP BY skill"""
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
    # Statistics travel inside the file, so the browser's planner sees
    # the same numbers this one does and picks the same plans the tests
    # check.
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


def db_key(built_at: str, digest: str) -> str:
    """explore/20260910T1417-0883d1e7.db: sorts by time, unique by content."""
    stamp = built_at.replace("-", "").replace(":", "")[:13]
    return f"{PREFIX}{stamp}-{digest[:8]}.db"


def stale_keys(keys: list[str], keep: int = KEEP) -> list[str]:
    """The builds to delete: everything but the newest `keep`, by name."""
    ours = sorted(k for k in keys if k.startswith(PREFIX) and k.endswith(".db"))
    return ours[:-keep] if len(ours) > keep else []


def _fresh_enough(s3, bucket: str) -> bool:
    """True when the manifest was written less than MAX_AGE_S ago."""
    try:
        head = s3.head_object(Bucket=bucket, Key=MANIFEST)
    except Exception:
        return False
    age = datetime.now(timezone.utc) - head["LastModified"]
    return age.total_seconds() < MAX_AGE_S


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
        out = work_dir / "explore.db"
        summary = build(snapshot, out)
        digest = hashlib.sha256()
        with open(out, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        key = db_key(summary["built_at"], digest.hexdigest())
        # application/octet-stream, and it matters: CloudFront compresses
        # compressible types on the fly, and compression and range
        # requests do not mix. A binary type is left alone, so the
        # runtime's byte offsets mean what it thinks they mean. The key
        # is unique to this build, so the object can be cached forever.
        s3.upload_file(
            str(out), frontend_bucket, key,
            ExtraArgs={"ContentType": "application/octet-stream",
                       "CacheControl": "public, max-age=31536000, immutable"},
        )
        manifest = {"url": "/" + key, "bytes": summary["bytes"], **{k: v for k, v in summary.items() if k != "bytes"}}
        # The one URL that changes. no-cache: the browser revalidates it
        # on every page load and always lands on the current build.
        s3.put_object(
            Bucket=frontend_bucket, Key=MANIFEST,
            Body=json.dumps(manifest).encode("utf-8"),
            ContentType="application/json", CacheControl="no-cache",
        )
        # Then the builds nobody can reach any more. A tab that opened an
        # older copy keeps its URL for as long as KEEP builds allow.
        listing = s3.list_objects_v2(Bucket=frontend_bucket, Prefix=PREFIX)
        old = stale_keys([o["Key"] for o in listing.get("Contents", [])])
        if old:
            s3.delete_objects(Bucket=frontend_bucket, Delete={"Objects": [{"Key": k} for k in old], "Quiet": True})
        print(f"published {key}: {json.dumps(summary)}; removed {len(old)} older", file=sys.stderr)
        return summary
    except Exception as e:
        print(f"couldn't build or publish {PREFIX} (non-fatal): {e!r}", file=sys.stderr)
        return None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("explore.db"))
    args = ap.parse_args()
    print(json.dumps(build(args.snapshot, args.out), indent=2))
