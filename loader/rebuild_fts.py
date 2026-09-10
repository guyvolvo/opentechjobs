"""Recreate the search index so that retiring a listing can remove it.

jobs_fts is contentless: it stores an inverted index and never the text.
That is what keeps the snapshot small, and it means a row can only be
dropped by rowid, and only if the table was created with
contentless_delete=1 on SQLite 3.43+. This snapshot's table predates
that flag, so archive.py has to hold back every listing that carries an
index entry rather than orphan its terms. Measured on the live file,
that is 49% of the rows a prune would otherwise take.

The terms cannot be recovered from the index, but the text they came
from is still there: every description lives as its own S3 object
(loader/descriptions.py), which is exactly why dropping the column was
safe. So this reads them back and indexes them into a new table that
has the flag.

Built into a new table and swapped in at the end, not edited in place.
A failure anywhere before the swap leaves the existing index untouched
and searchable, which matters because there is no way to rebuild it from
a half-finished one.

Runs inside the applier so it inherits the conditional push: it pulls
the snapshot, rebuilds, and pushes with If-Match, so a concurrent write
loses the race cleanly instead of clobbering. A standalone script would
have to fight the 5-minute cycle for minutes at a time.
"""

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

from descriptions import get_one

# Blobs fetched and indexed per batch. Descriptions average about 10KB,
# so holding all 102,000 at once is a gigabyte and holding a thousand is
# ten megabytes. The batch is the whole reason this fits in a Lambda.
BATCH = 1000
WORKERS = 32


def already_supported(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs_fts'"
    ).fetchone()
    return bool(row) and "contentless_delete" in (row[0] or "")


def rebuild(conn: sqlite3.Connection, bucket: str, log=print, s3=None) -> dict:
    """Returns a summary. Raises only if the swap itself fails, which is
    the one point where the old index is already gone.
    """
    if already_supported(conn):
        return {"skipped": "jobs_fts already supports contentless_delete"}
    if not bucket:
        return {"skipped": "no bucket configured"}
    if sqlite3.sqlite_version_info < (3, 43):
        return {"skipped": f"sqlite {sqlite3.sqlite_version} predates contentless_delete"}

    if s3 is None:
        import boto3

        s3 = boto3.client("s3")

    conn.execute("DROP TABLE IF EXISTS jobs_fts_rebuild")
    conn.execute("CREATE VIRTUAL TABLE jobs_fts_rebuild USING fts5("
                 "description, content='', contentless_delete=1)")

    # description_sha is the marker that a blob was ever written for this
    # listing. Rows without one never had a description to index.
    targets = conn.execute(
        "SELECT rowid, id FROM jobs WHERE description_sha IS NOT NULL AND description_sha != ''"
    ).fetchall()
    log(f"rebuilding search index over {len(targets):,} listings")

    indexed = missing = 0
    for start in range(0, len(targets), BATCH):
        batch = targets[start:start + BATCH]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            texts = list(pool.map(lambda r: get_one(bucket, r["id"], s3), batch))
        rows = [(r["rowid"], t) for r, t in zip(batch, texts) if t]
        missing += len(batch) - len(rows)
        conn.executemany(
            "INSERT INTO jobs_fts_rebuild(rowid, description) VALUES (?, ?)", rows)
        indexed += len(rows)
        if start % (BATCH * 10) == 0:
            log(f"  {indexed:,}/{len(targets):,} indexed")

    # The only irreversible moment. Everything above can fail harmlessly.
    conn.execute("DROP TABLE jobs_fts")
    conn.execute("ALTER TABLE jobs_fts_rebuild RENAME TO jobs_fts")
    log(f"search index rebuilt: {indexed:,} indexed, {missing:,} with no blob to read")
    return {"indexed": indexed, "missing_blobs": missing, "targets": len(targets)}


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--bucket", required=True)
    args = ap.parse_args()
    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    print(rebuild(c, args.bucket))
    c.commit()
    c.close()
