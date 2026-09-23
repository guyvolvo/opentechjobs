"""Publish one compacted copy of the database to S3, once a day.

The box's applier writes in place and never uploads, which is the whole
point: the Lambda applier's 600MB multipart push every five minutes was
1.49 million S3 PUTs a month and most of the bill. But two things still
read jobs-read.db out of S3 and would break silently without it:

  - .github/workflows/build-salary-matrix.yml
  - backfill_descriptions.py, run by hand

So they get a daily copy instead of a five-minutely one, which is ~75
PUTs a day rather than ~21,600, and is fresh enough for both: one reads
a salary distribution over months of listings, the other backfills text
that is already in S3.

VACUUM INTO rather than a file copy. It reads through SQLite so it
cannot catch a torn page mid-write, it drops the free pages an applier
leaves behind, and it never blocks the writer, unlike a plain VACUUM.
The cost is reading the whole database and writing a compacted one, so
this runs on its own timer and not inside an apply.

Doubles as a point-in-time backup beside Litestream's continuous WAL
stream: different mechanism, different failure modes, and this one is a
plain file anybody can open.

    python box/publish_snapshot.py
"""

import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig

DB = Path(os.environ.get("DATA_PATH", "/var/lib/otj/jobs.db"))
BUCKET = os.environ["DATA_BUCKET"]
KEY = os.environ.get("SNAPSHOT_KEY", "jobs-read.db")
OUT = DB.with_name("jobs-read.publish.db")
# 16MB parts rather than the default 8MB: half the PUTs for the same
# bytes, and nothing here is racing a timeout.
_TRANSFER = TransferConfig(multipart_chunksize=16 * 1024 * 1024, max_concurrency=4)


def main() -> int:
    started = time.monotonic()
    OUT.unlink(missing_ok=True)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    try:
        conn.execute("VACUUM INTO ?", (str(OUT),))
    finally:
        conn.close()
    vacuum_s = time.monotonic() - started

    check = sqlite3.connect(f"file:{OUT}?mode=ro", uri=True)
    try:
        rows = check.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        ok = check.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok" or rows == 0:
        # Never push something that failed its own check: the readers of
        # this key have no way to tell a bad snapshot from a good one.
        print(f"refusing to publish: quick_check={ok}, rows={rows}", file=sys.stderr)
        OUT.unlink(missing_ok=True)
        return 1

    size = OUT.stat().st_size
    s3 = boto3.client("s3")
    s3.upload_file(str(OUT), BUCKET, KEY, Config=_TRANSFER)
    # A dated copy too, which the bucket's own lifecycle rule expires
    # after seven days (see infra/s3.tf's backups/ rule).
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    s3.copy_object(Bucket=BUCKET, Key=f"backups/jobs-read-{day}.db",
                   CopySource={"Bucket": BUCKET, "Key": KEY})
    OUT.unlink(missing_ok=True)
    print(f"published {rows:,} rows, {size:,} bytes to s3://{BUCKET}/{KEY} "
          f"(vacuum {vacuum_s:.0f}s, total {time.monotonic() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
