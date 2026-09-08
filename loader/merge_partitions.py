#!/usr/bin/env python3
"""
Build jobs-read.db, the API's read-only snapshot, by merging every
writer's own small jobs-partition-{name}.db back into one file. Companion
to load_to_sqlite.py's --key/--skip-known flags -- see the Partition &
Merge design doc for the full picture: scrape_handler.py and
scrape_workday_handler.py each write to their OWN partition file, so their
own S3 I/O cost scales with their own slice of work, not jobs.db's
ever-growing total size. This script is the once-daily step that combines
those partitions back into the single file api/db.py actually reads.

Two kinds of partition, told apart by name:
  - Numbered (jobs-partition-{shard_index}.db): scrape_handler.py's own
    sharded fast-poll. A company's shard assignment is recomputed here
    from the CURRENT known.json via sharding.py -- the same math
    scrape_handler.py itself uses to decide where to WRITE a company, so
    the two never disagree. A company that's since moved shards (NUM_SHARDS
    grew) or been pruned entirely (gone from known.json) has its stale row
    in this partition dropped here instead of merged in a second time.
  - Named (jobs-partition-{name}.db, name not a plain integer -- e.g.
    "workday"): a pinned-company Lambda's own partition. No shard
    reassignment concept applies there -- companies.yml, not known.json,
    decides membership -- so every row is trusted as-is.

merge_partitions() itself takes local file paths and touches no network at
all, so it's directly unit-testable; main() owns the actual S3
list/pull/push cycle around it.

Usage:
    python merge_partitions.py --bucket iljobs-data --out jobs-read.db

Dependencies: boto3 (S3), same as load_to_sqlite.py's own --bucket mode.
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

# sharding.py lives at the repo root alongside the other scrape_*_handler.py
# entry points, not in loader/ next to this script, so (unlike
# load_to_sqlite.py, same directory, resolves on its own) it needs an
# explicit path add.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from load_to_sqlite import SCHEMA_VERSION, open_db, s3_pull, s3_push, update_meta  # noqa: E402
from sharding import build_shard_map  # noqa: E402

PARTITION_PREFIX = "jobs-partition-"
_NUMBERED = re.compile(r"^\d+$")


def list_partitions(bucket: str, prefix: str = PARTITION_PREFIX) -> list[str]:
    import boto3

    s3 = boto3.client("s3")
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".db"):
                keys.append(obj["Key"])
    return sorted(keys)


def _partition_name(key: str, prefix: str) -> str:
    return key[len(prefix):-len(".db")]


def merge_partitions(known: list[dict], partition_paths: dict[str, Path], out_path: Path) -> dict:
    """partition_paths maps each partition's own name (e.g. "0", "1",
    "workday" -- not the S3 key or local filename) to its already-
    downloaded local file. Builds a fresh DB at out_path and returns a
    summary dict for logging.
    """
    shard_map = build_shard_map(known)

    if out_path.exists():
        out_path.unlink()
    merged = open_db(out_path)

    summary: dict = {"partitions": [], "companies_merged": 0, "companies_dropped_stale": 0,
                      "skipped_version_mismatch": []}

    # Numbered (shard) partitions first, named (pinned) partitions last:
    # a pinned Lambda's own dedicated re-poll (Workday's, say) is the more
    # authoritative source for a domain that happens to appear in both
    # known.json and companies.yml, so it should win the INSERT OR REPLACE
    # collision, not lose to whichever partition merged first.
    def sort_key(name: str):
        return (0, int(name)) if _NUMBERED.match(name) else (1, name)

    for name in sorted(partition_paths, key=sort_key):
        local_path = partition_paths[name]
        part_conn = sqlite3.connect(local_path)
        version = part_conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            part_conn.close()
            print(f"SCHEMA MISMATCH: partition {name!r} is user_version={version}, expected "
                  f"{SCHEMA_VERSION} -- skipping it entirely this run, not merging its rows",
                  file=sys.stderr)
            summary["skipped_version_mismatch"].append(name)
            continue

        all_domains = [r[0] for r in part_conn.execute("SELECT domain FROM companies")]
        if _NUMBERED.match(name):
            shard_index = int(name)
            # A domain not in shard_map at all (pruned from known.json
            # since this partition last wrote) is dropped the same way as
            # one that's moved to a different shard -- .get() returns
            # None either way, which never equals a real shard_index.
            keep_domains = [d for d in all_domains if shard_map.get(d) == shard_index]
        else:
            keep_domains = all_domains  # pinned partition, no reassignment concept
        part_conn.close()

        dropped = len(all_domains) - len(keep_domains)
        summary["companies_dropped_stale"] += dropped
        summary["companies_merged"] += len(keep_domains)
        summary["partitions"].append({"name": name, "kept": len(keep_domains), "dropped": dropped})

        if not keep_domains:
            continue

        placeholders = ",".join("?" for _ in keep_domains)
        merged.execute("ATTACH DATABASE ? AS src", (str(local_path),))
        # DETACH has to come after the transaction that touched src
        # commits -- see the design doc's "Considered and declined" note
        # on the same mistake in an earlier reviewed SQL snippet.
        with merged:
            merged.execute(
                f"INSERT OR REPLACE INTO companies SELECT * FROM src.companies WHERE domain IN ({placeholders})",
                keep_domains,
            )
            merged.execute(
                f"INSERT OR REPLACE INTO jobs SELECT * FROM src.jobs WHERE company_domain IN ({placeholders})",
                keep_domains,
            )
        merged.execute("DETACH DATABASE src")

    # with merged: commits -- VACUUM can't run inside an open transaction,
    # and update_meta()'s own INSERTs (unlike the ATTACH/INSERT blocks
    # above, which commit themselves via their own `with merged:`) leave
    # one open otherwise. Same pattern load_to_sqlite.py's own main() uses
    # around its update_meta() call, for the same reason.
    with merged:
        update_meta(merged)
    # Plain VACUUM in place, not the design doc's literal "VACUUM INTO a
    # fresh file" -- merged was already opened fresh at out_path (open_db
    # above), so there's no separate staging file to fold in; VACUUM here
    # reclaims the same intermediate free space either phrasing would.
    merged.execute("VACUUM")
    merged.close()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True, help="S3 bucket holding the partitions and known.json")
    ap.add_argument("--out", required=True, type=Path, help="local path to build jobs-read.db at")
    ap.add_argument("--key", default="jobs-read.db", help="S3 key to push the merged DB to (default: jobs-read.db)")
    ap.add_argument("--prefix", default=PARTITION_PREFIX,
                     help=f"S3 key prefix identifying a partition file (default: {PARTITION_PREFIX})")
    ap.add_argument("--tmp-dir", type=Path, default=Path("/tmp"), help="scratch dir for downloaded partitions")
    args = ap.parse_args()

    known_path = args.tmp_dir / "known-for-merge.json"
    existed, _ = s3_pull(args.bucket, "known.json", known_path)
    known = json.loads(known_path.read_text(encoding="utf-8")) if existed else []
    if not known:
        # Refuse rather than merge: build_shard_map([]) would map every
        # domain to nothing, so every numbered-shard partition's rows
        # would look stale and get dropped -- a transient known.json pull
        # failure would silently gut jobs-read.db instead of just
        # skipping this run and leaving the last good snapshot in place.
        print("known.json is empty or missing -- refusing to merge, leaving the existing "
              f"s3://{args.bucket}/{args.key} untouched", file=sys.stderr)
        return 1

    keys = list_partitions(args.bucket, args.prefix)
    if not keys:
        print(f"no {args.prefix}*.db partitions found in s3://{args.bucket} -- nothing to merge", file=sys.stderr)
        return 1

    partition_paths = {}
    for key in keys:
        name = _partition_name(key, args.prefix)
        local_path = args.tmp_dir / f"partition-{name}.db"
        existed, _ = s3_pull(args.bucket, key, local_path)
        if existed:
            partition_paths[name] = local_path
        else:
            print(f"{key} was listed but gone on pull (another process's own cleanup?) -- skipping",
                  file=sys.stderr)

    summary = merge_partitions(known, partition_paths, args.out)
    print(json.dumps(summary), file=sys.stderr)
    if summary["skipped_version_mismatch"]:
        print(f"ALERT: {len(summary['skipped_version_mismatch'])} partition(s) skipped for schema "
              f"mismatch: {summary['skipped_version_mismatch']}", file=sys.stderr)

    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)", file=sys.stderr)
    s3_push(args.bucket, args.key, args.out)
    print(f"pushed {args.out} to s3://{args.bucket}/{args.key}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
