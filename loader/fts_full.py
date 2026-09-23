"""Build the five-column search index, once, on the box.

jobs_fts used to index description text alone, and the four metadata
columns were matched with LIKE '%term%' on every row of every search:
four substring scans over 846k rows per term, which is where 3.5 to 4
seconds of every search went, and why "rust" answered with Trustly.
This table carries title, company_domain, location and department
beside the description, so one MATCH answers the whole search off the
index, whole words only.

Contentless, like its predecessor: the index and never the text. The
metadata is a few hundred bytes a row and lives in jobs anyway; the
description text is in S3 (loader/descriptions.py) and is read back
from there, one blob per listing, which is the slow part of this run
and the reason it happens once.

Built into jobs_fts_full and swapped in at the end, so the existing
index stays searchable until the new one is whole. Then meta
fts_complete is set, which is what api/job_filters.has_fts_index()
waits for before it trusts any index at all: an index that exists but
is not marked complete is not used. After this, load_to_sqlite's
index_description keeps the table current row by row.

    python loader/fts_full.py --db /var/lib/otj/jobs.db --bucket $DATA_BUCKET

Commits every batch. The applier can be paused for the hour this
takes, or left running: each batch holds the write lock for well under
its busy timeout.
"""

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from descriptions import PREFIX, get_one  # noqa: E402

BATCH = 1000
WORKERS = 48


def _ids_with_text(s3, bucket: str) -> set[str]:
    """Every listing that has a blob, from one listing of the prefix.
    Same reasoning as rebuild_fts.py: it is exact, and it is a few
    hundred calls instead of a GET per row that does not have one."""
    out: set[str] = set()
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            out.add(obj["Key"][len(PREFIX):].removesuffix(".json"))
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated"):
            break
    return out


def build(conn: sqlite3.Connection, bucket: str, log=print) -> dict:
    import boto3
    from botocore.config import Config

    s3 = boto3.client("s3", config=Config(max_pool_connections=WORKERS))
    started = time.monotonic()
    with_text = _ids_with_text(s3, bucket)
    log(f"{len(with_text):,} listings have a description in S3")

    conn.execute("DROP TABLE IF EXISTS jobs_fts_full")
    conn.execute(
        "CREATE VIRTUAL TABLE jobs_fts_full USING fts5("
        "title, company_domain, location, department, description, "
        "content='', contentless_delete=1)"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT rowid, id, title, company_domain, location, department FROM jobs ORDER BY rowid"
    ).fetchall()
    log(f"indexing {len(rows):,} listings, {sum(1 for r in rows if r['id'] in with_text):,} with text")

    indexed = with_blob = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        need = [r for r in batch if r["id"] in with_text]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            texts = dict(zip((r["id"] for r in need),
                             pool.map(lambda r: get_one(bucket, r["id"], s3), need)))
        conn.executemany(
            "INSERT INTO jobs_fts_full(rowid, title, company_domain, location, department, description)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(r["rowid"], r["title"], r["company_domain"], r["location"], r["department"],
              texts.get(r["id"]) or "") for r in batch],
        )
        conn.commit()
        indexed += len(batch)
        with_blob += sum(1 for r in need if texts.get(r["id"]))
        if start % (BATCH * 20) == 0:
            elapsed = time.monotonic() - started
            log(f"  {indexed:,}/{len(rows):,} indexed, {with_blob:,} with text, {elapsed:.0f}s")

    # The swap. A reader between these two statements sees no jobs_fts
    # and has_fts_index() answers False, which is the safe answer.
    conn.execute("DROP TABLE IF EXISTS jobs_fts")
    conn.execute("ALTER TABLE jobs_fts_full RENAME TO jobs_fts")
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('fts_complete', '1')"
        " ON CONFLICT(key) DO UPDATE SET value = '1'"
    )
    # This file carries a real index again, so open_db stops stripping
    # jobs_fts out of the schema (load_to_sqlite._fts_retired).
    conn.execute("DELETE FROM meta WHERE key = 'fts_retired'")
    conn.commit()
    elapsed = time.monotonic() - started
    log(f"search index built: {indexed:,} rows, {with_blob:,} with text, {elapsed:.0f}s")
    return {"indexed": indexed, "with_text": with_blob, "seconds": round(elapsed)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--bucket", required=True)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        build(conn, args.bucket)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
