#!/usr/bin/env python3
"""
One-time Partition & Merge migration (2026-09-08). Splits the current,
single jobs.db into the per-shard jobs-partition-{N}.db files scrape_
handler.py's own sharding now expects, plus a pinned jobs-partition-
workday.db for companies.yml's own companies, and builds the starting
jobs-read.db snapshot from them -- via merge_partitions.py's own tested
merge_partitions(), not a separate one-off copy, so the very first
jobs-read.db is built by the exact code path that will run every hour
from then on.

Run this ONCE, locally, before deploying the writer/API changes that
expect jobs-read.db and the partitions to already exist -- see the
Partition & Merge design doc's own migration step. Writes local files
only unless --push is given; inspect --out-dir's contents first.

A company not in known.json and not one of companies.yml's own workday
pins (e.g. something --prune-stale would have demoted on the next full
discover run, or a genuinely orphaned row) falls into the workday
partition too, as a catch-all, rather than being silently dropped --
flagged explicitly in this script's own output so a human can decide
whether it's worth cleaning up. Going forward only scrape_workday_
handler.py writes that partition, so such a row would simply sit there
unchanged until someone notices.

Usage:
    python migrate_to_partitions.py --jobs-db jobs.db --known known.json \\
        --companies-yml ../companies.yml --out-dir ./migration-output

    # inspect ./migration-output, THEN, to actually publish:
    python migrate_to_partitions.py --jobs-db jobs.db --known known.json \\
        --companies-yml ../companies.yml --out-dir ./migration-output \\
        --push --bucket iljobs-data-876913698688
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # sharding.py, repo root

from load_to_sqlite import open_db, s3_push  # noqa: E402
from merge_partitions import merge_partitions  # noqa: E402
from sharding import build_shard_map  # noqa: E402
from probe import load_pins  # noqa: E402 -- reuse the real parser, not a second copy of it


def load_workday_pins(companies_yml: Path) -> set[str]:
    return set(load_pins(companies_yml).get("workday", {}).keys())


def split_into_partitions(jobs_db: Path, known: list[dict], workday_pins: set[str], out_dir: Path) -> dict[str, Path]:
    shard_map = build_shard_map(known)

    # open_db(), not a raw sqlite3.connect(): applies schema.sql's CREATE
    # TABLE IF NOT EXISTS + _migrate()'s column backfill first. jobs.db
    # has been through that on every regular write already, but this
    # script shouldn't assume that -- an ATTACH below against a
    # not-yet-migrated file would fail with a column-count mismatch
    # against the destination's current schema (caught locally testing
    # this against an intentionally stale copy).
    src = open_db(jobs_db)
    domains = [r["domain"] for r in src.execute("SELECT domain FROM companies")]

    by_partition: dict[str, list[str]] = {}
    fallback = []
    for d in domains:
        if d in workday_pins:
            name = "workday"
        elif d in shard_map:
            name = str(shard_map[d])
        else:
            name = "workday"  # catch-all -- see this module's own docstring
            fallback.append(d)
        by_partition.setdefault(name, []).append(d)
    src.close()

    if fallback:
        print(f"WARNING: {len(fallback)} companies are in jobs.db but neither known.json nor "
              f"companies.yml's workday pins -- routed into the workday partition as a catch-all, "
              f"won't be re-checked by anything going forward until a human looks at them: "
              f"{fallback}", file=sys.stderr)

    out_dir.mkdir(parents=True, exist_ok=True)
    partition_paths: dict[str, Path] = {}
    for name, domain_list in by_partition.items():
        dest = out_dir / f"jobs-partition-{name}.db"
        if dest.exists():
            dest.unlink()
        conn = open_db(dest)  # fresh schema + PRAGMA user_version stamp
        conn.execute("ATTACH DATABASE ? AS src", (str(jobs_db),))
        placeholders = ",".join("?" for _ in domain_list)
        # Named column lists, not SELECT * -- caught testing this locally:
        # jobs.db carries a legacy description_snippet column (see
        # load_to_sqlite.py's own _migrate() docstring) that an ADD-
        # COLUMN-only migration strategy can never retroactively drop, so
        # a real production jobs.db has one column more than this fresh
        # destination's own current schema expects. See merge_partitions.
        # py's own _table_columns() -- same fix, same reasoning.
        company_cols = [r[1] for r in conn.execute("PRAGMA table_info(companies)")]
        job_cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
        with conn:
            conn.execute(
                f"INSERT OR REPLACE INTO companies ({','.join(company_cols)}) "
                f"SELECT {','.join(company_cols)} FROM src.companies WHERE domain IN ({placeholders})",
                domain_list,
            )
            conn.execute(
                f"INSERT OR REPLACE INTO jobs ({','.join(job_cols)}) "
                f"SELECT {','.join(job_cols)} FROM src.jobs WHERE company_domain IN ({placeholders})",
                domain_list,
            )
        conn.execute("DETACH DATABASE src")
        conn.execute("VACUUM")
        conn.close()
        partition_paths[name] = dest
        print(f"jobs-partition-{name}.db: {len(domain_list)} companies, "
              f"{dest.stat().st_size / 1_048_576:.1f}MB", file=sys.stderr)

    return partition_paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-db", required=True, type=Path, help="local copy of the current, live jobs.db")
    ap.add_argument("--known", required=True, type=Path, help="local copy of the current, live known.json")
    ap.add_argument("--companies-yml", required=True, type=Path, help="path to companies.yml, for workday pins")
    ap.add_argument("--out-dir", required=True, type=Path, help="where to write the split partitions + jobs-read.db")
    ap.add_argument("--push", action="store_true", help="also upload everything in --out-dir to S3")
    ap.add_argument("--bucket", help="required if --push")
    args = ap.parse_args()

    if args.push and not args.bucket:
        print("--push requires --bucket", file=sys.stderr)
        return 1

    known = json.loads(args.known.read_text(encoding="utf-8"))
    workday_pins = load_workday_pins(args.companies_yml)
    print(f"{len(known)} known companies, {len(workday_pins)} workday pins", file=sys.stderr)

    partition_paths = split_into_partitions(args.jobs_db, known, workday_pins, args.out_dir)

    out_path = args.out_dir / "jobs-read.db"
    summary = merge_partitions(known, partition_paths, out_path)
    print(f"jobs-read.db: {json.dumps(summary)}", file=sys.stderr)
    print(f"jobs-read.db: {out_path.stat().st_size / 1_048_576:.1f}MB", file=sys.stderr)

    if summary["skipped_version_mismatch"]:
        print("ERROR: a freshly-built partition failed its own schema-version check -- "
              "this should be impossible (open_db() just stamped it) -- stopping before any push",
              file=sys.stderr)
        return 1

    if not args.push:
        print(f"\nDry run only -- files are in {args.out_dir}, nothing pushed to S3. "
              f"Re-run with --push --bucket <bucket> once you've inspected them.", file=sys.stderr)
        return 0

    for name, path in partition_paths.items():
        key = f"jobs-partition-{name}.db"
        s3_push(args.bucket, key, path)
        print(f"pushed {key}", file=sys.stderr)
    s3_push(args.bucket, "jobs-read.db", out_path)
    print("pushed jobs-read.db", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
