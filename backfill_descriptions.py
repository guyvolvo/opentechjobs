"""One-time: copy every description already in the snapshot out to S3.

New and changed descriptions have been written as blobs since commit 1,
but everything scraped before that exists only inside jobs-read.db. This
walks the snapshot and uploads whatever has no blob yet, so the column
can be dropped without losing text that would otherwise need a full
re-scrape to recover.

Safe to stop and re-run. Nothing is deleted, uploads are idempotent
(same key, same content), and --skip-existing lists what is already there
so a resumed run does not pay for the same objects twice.

    python backfill_descriptions.py --bucket $DATA_BUCKET --db jobs-read.db
    python backfill_descriptions.py --bucket $DATA_BUCKET --db jobs-read.db --dry-run
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "loader"))

from descriptions import PREFIX, description_sha, put_many  # noqa: E402


def existing_keys(bucket: str) -> set[str]:
    """Job ids that already have a blob, so a resumed run skips them."""
    import boto3

    s3 = boto3.client("s3")
    seen: set[str] = set()
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".json"):
                seen.add(key[len(PREFIX):-len(".json")])
        if not resp.get("IsTruncated"):
            return seen
        token = resp["NextContinuationToken"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, type=Path, help="local jobs-read.db to read descriptions from")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--batch", type=int, default=500, help="uploads per batch, for progress reporting")
    ap.add_argument("--limit", type=int, default=0, help="stop after N uploads (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="count and size the work, upload nothing")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, description FROM jobs WHERE description IS NOT NULL AND description != ''"
    ).fetchall()
    conn.close()
    total_chars = sum(len(r["description"]) for r in rows)
    print(f"{len(rows):,} jobs carry a description, {total_chars/1024**2:.1f} MB of text", file=sys.stderr)

    have = set() if args.dry_run else existing_keys(args.bucket)
    todo = [(r["id"], r["description"]) for r in rows if r["id"] not in have]
    print(f"{len(have):,} already uploaded, {len(todo):,} to go", file=sys.stderr)
    if args.limit:
        todo = todo[:args.limit]

    if args.dry_run:
        # S3 PUT is $0.005 per 1,000 requests.
        print(f"dry run: would upload {len(todo):,} objects, about ${len(todo)/1000*0.005:.2f} in PUTs",
              file=sys.stderr)
        return 0

    done = 0
    started = time.time()
    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        done += put_many(args.bucket, chunk)
        rate = done / max(1e-9, time.time() - started)
        print(f"  {done:,}/{len(todo):,} uploaded ({rate:.0f}/s)", file=sys.stderr)
    print(f"backfilled {done:,} descriptions in {time.time()-started:.0f}s", file=sys.stderr)
    return 0 if done == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
