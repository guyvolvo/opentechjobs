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

A company can legitimately appear in more than one partition at once --
scrape_handler.py's own shard assignment (sort every known domain, slice
into fixed-size chunks) shifts for a large fraction of ALL companies
every time NUM_SHARDS grows by one, which -- confirmed live, 2026-09-08,
the day this shipped -- happens roughly every 50 newly-discovered
companies, i.e. every several minutes given this project's own discovery
pace, far more often than a full shard rotation completes. An earlier
version of this function tried to resolve that by recomputing each
company's "current" shard from a freshly-downloaded known.json and
dropping any partition's row that didn't match -- correct in spirit
(tombstone a company's stale copy in a partition it's moved away from),
but the "current" shard number itself turned out to be too volatile to
trust: it live-dropped 1,476 of 2,482 companies (59%) on this feature's
very first real merge, because most companies hadn't yet been rewritten
under their newest, still-shifting assignment by the time this ran.

Fixed the same day by dropping the formula entirely: when a domain
appears in multiple partitions, this now just keeps whichever copy has
the more recent companies.last_checked (every writer already stamps this
on every upsert). That's a strictly more robust way to reach the exact
same goal -- a company's freshest write always wins, regardless of which
partition physically holds it, with no formula to drift out from under
itself. It also subsumes what used to be special pinned-partition
handling (jobs-partition-workday.db beating a numbered shard on a
collision): a pinned Lambda's own re-poll keeps last_checked genuinely
current, so it wins on its own merits, not because its partition name
was hardcoded to win.

merge_partitions() itself takes local file paths and touches no network at
all, so it's directly unit-testable; main() owns the actual S3
list/pull/push cycle around it.

Usage:
    python merge_partitions.py --bucket iljobs-data --out jobs-read.db

Dependencies: boto3 (S3), same as load_to_sqlite.py's own --bucket mode.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# sharding.py lives at the repo root alongside the other scrape_*_handler.py
# entry points, not in loader/ next to this script, so (unlike
# load_to_sqlite.py, same directory, resolves on its own) it needs an
# explicit path add.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from load_to_sqlite import SCHEMA_VERSION, open_db, s3_pull, s3_push, update_meta  # noqa: E402

PARTITION_PREFIX = "jobs-partition-"


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


def _table_columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _shared_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Columns present in BOTH the merged output and the attached source,
    in the output's own order. Requires src to be ATTACHed.
    """
    src = set(_table_columns(conn, table, schema="src"))
    return [c for c in _table_columns(conn, table) if c in src]


def apply_company_names(conn: sqlite3.Connection, names: dict[str, str] | None) -> int:
    """Stamp each company's real name onto the merged snapshot.

    Names live in their own file (company-names.json, written by
    resolve_company_names.py) rather than in the partitions, because they
    come from a completely different cadence: a name is resolved once and
    never again, while partitions are rewritten every rotation. Applying
    them here means a newly resolved name reaches the site on the next
    merge without needing every partition rewritten first.

    Only ever fills a name in, never clears one: a company missing from
    the file keeps whatever it already had.
    """
    if not names:
        return 0
    rows = [(name, domain) for domain, name in names.items() if name]
    if not rows:
        return 0
    with conn:
        conn.executemany("UPDATE companies SET company_name = ? WHERE domain = ?", rows)
    return conn.execute(
        "SELECT COUNT(*) FROM companies WHERE company_name IS NOT NULL"
    ).fetchone()[0]


def merge_partitions(partition_paths: dict[str, Path], out_path: Path,
                     names: dict[str, str] | None = None) -> dict:
    """partition_paths maps each partition's own name (e.g. "0", "1",
    "workday" -- not the S3 key or local filename) to its already-
    downloaded local file. Builds a fresh DB at out_path and returns a
    summary dict for logging.
    """
    if out_path.exists():
        out_path.unlink()
    # Delete first, always. open_db() opens whatever is already at this
    # path, and on a WARM Lambda container that's the previous
    # invocation's own finished jobs-read.db, roughly 1.2GB of it --
    # every INSERT OR REPLACE below then rewrites rows into an
    # already-populated file instead of appending to an empty one.
    # Confirmed live (2026-09-09): that fragmentation is what drove Max
    # Memory Used to 2996MB against a hard 3008MB ceiling (99.6%, with
    # no headroom purchasable -- see infra/variables.tf) and median
    # duration from ~30s to 122s over a single day. A cold container hid
    # it completely, which is why it read as data growth rather than a
    # bug. Building fresh every run makes both numbers a function of
    # today's data only, not of how long this container has been warm.
    out_path.unlink(missing_ok=True)
    merged = open_db(out_path)

    summary: dict = {"partitions": [], "companies_merged": 0, "companies_superseded": 0,
                      "skipped_version_mismatch": []}

    # Pass 1: for every schema-current partition, find each domain's own
    # last_checked, and track which partition holds the most recent one.
    # A read-only inspection pass -- these connections close before the
    # ATTACH-based copy below opens its own handle onto the same files.
    valid_names: list[str] = []
    total_rows_by_partition: dict[str, int] = {}
    winner_of: dict[str, tuple[str, str]] = {}  # domain -> (partition_name, last_checked)
    for name, local_path in partition_paths.items():
        conn = sqlite3.connect(local_path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            conn.close()
            print(f"SCHEMA MISMATCH: partition {name!r} is user_version={version}, expected "
                  f"{SCHEMA_VERSION} -- skipping it entirely this run, not merging its rows",
                  file=sys.stderr)
            summary["skipped_version_mismatch"].append(name)
            continue
        valid_names.append(name)
        rows = conn.execute("SELECT domain, last_checked FROM companies").fetchall()
        total_rows_by_partition[name] = len(rows)
        for domain, last_checked in rows:
            current = winner_of.get(domain)
            if current is None or last_checked > current[1]:
                winner_of[domain] = (name, last_checked)
        conn.close()

    # Pass 2: for each partition, keep only the domains it actually won.
    keep_by_partition: dict[str, list[str]] = {}
    for domain, (winner_name, _) in winner_of.items():
        keep_by_partition.setdefault(winner_name, []).append(domain)

    for name in valid_names:
        local_path = partition_paths[name]
        keep_domains = keep_by_partition.get(name, [])
        superseded = total_rows_by_partition[name] - len(keep_domains)
        summary["companies_merged"] += len(keep_domains)
        summary["companies_superseded"] += superseded
        summary["partitions"].append({"name": name, "kept": len(keep_domains), "superseded": superseded})

        if not keep_domains:
            continue

        placeholders = ",".join("?" for _ in keep_domains)
        merged.execute("ATTACH DATABASE ? AS src", (str(local_path),))
        # Named column lists, not SELECT * -- confirmed live testing this
        # against a real (if stale) jobs.db: production's own jobs table
        # carries a legacy description_snippet column (see load_to_sqlite.
        # py's own _migrate() docstring) that predates the current
        # schema.sql and that an ADD-COLUMN-only migration strategy can
        # never retroactively drop from an already-existing file. SELECT *
        # against a partition built from that history returns one column
        # too many for this connection's own (current-schema) INSERT to
        # accept. Column lists come from THIS connection's own live
        # schema, so they track schema.sql automatically -- a partition's
        # extra legacy columns are just ignored, not propagated forward.
        # Intersected with the SOURCE's own columns, not taken from this
        # connection alone. The comment above covers one direction, a
        # partition carrying a legacy column this schema no longer has,
        # and dropping it is correct. The other direction is the one that
        # breaks: the moment schema.sql GAINS a column, every partition
        # already sitting in S3 predates it, and selecting a column the
        # source doesn't have is an OperationalError that kills the whole
        # merge. That stops jobs-read.db updating at all and freezes the
        # site on stale data, silently, which is a far worse failure than
        # the missing column itself. Intersecting means an added column
        # simply arrives as NULL for older partitions and fills in as
        # each one gets rewritten by its own next scrape.
        company_cols = _shared_columns(merged, "companies")
        # description is deliberately dropped on the way in. It is ~94%
        # of this file's bytes and the text now lives as one S3 object
        # per job (loader/descriptions.py), read back by /api/jobs/{id}.
        # The words are still searchable: jobs_fts is rebuilt below from
        # the same source rows. Partitions keep the column, because
        # building the index needs the text.
        # description and raw_json are both dropped on the way in.
        # Measured on the live snapshot: raw_json was 583MB (48% of the
        # file) and description 516MB (42%), together 90% of it. Nothing
        # reads raw_json at all and every field in it except token is
        # already a column beside it; the description text now lives as
        # one S3 object per job, with the words still searchable through
        # jobs_fts, rebuilt below.
        _DROP = {"description", "raw_json"}
        job_cols = [c for c in _shared_columns(merged, "jobs") if c not in _DROP]
        # DETACH has to come after the transaction that touched src
        # commits -- see the design doc's "Considered and declined" note
        # on the same mistake in an earlier reviewed SQL snippet.
        with merged:
            merged.execute(
                f"INSERT OR REPLACE INTO companies ({','.join(company_cols)}) "
                f"SELECT {','.join(company_cols)} FROM src.companies WHERE domain IN ({placeholders})",
                keep_domains,
            )
            merged.execute(
                f"INSERT OR REPLACE INTO jobs ({','.join(job_cols)}) "
                f"SELECT {','.join(job_cols)} FROM src.jobs WHERE company_domain IN ({placeholders})",
                keep_domains,
            )
        # Rebuilt, never copied: FTS5 rowids only mean anything inside one
        # database file, and this output assigns fresh rowids as rows
        # arrive. Joining on the stable job id maps each one correctly.
        with merged:
            merged.execute(
                "INSERT INTO jobs_fts(rowid, description) "
                "SELECT j.rowid, s.description FROM jobs j JOIN src.jobs s ON s.id = j.id "
                "WHERE s.description IS NOT NULL AND s.description != ''"
            )
        merged.execute("DETACH DATABASE src")

    # with merged: commits -- VACUUM can't run inside an open transaction,
    # and update_meta()'s own INSERTs (unlike the ATTACH/INSERT blocks
    # above, which commit themselves via their own `with merged:`) leave
    # one open otherwise. Same pattern load_to_sqlite.py's own main() uses
    # around its update_meta() call, for the same reason.
    with merged:
        update_meta(merged)
    apply_company_names(merged, names)

    # No VACUUM. It used to run here to reclaim free pages, but with the
    # unlink above out_path really is a fresh file that only ever gets
    # sequential INSERTs, so there are essentially no free pages to
    # reclaim -- it was rebuilding the whole ~1.2GB file to recover
    # almost nothing, and it was the single most expensive step in the
    # merge. The comment it replaces claimed "merged was already opened
    # fresh at out_path", which was true only on a cold container and is
    # what made the leftover-file bug so easy to miss. Put it back only
    # if jobs-read.db's own on-disk size starts drifting above the sum of
    # its partitions.
    merged.close()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True, help="S3 bucket holding the partitions")
    ap.add_argument("--out", required=True, type=Path, help="local path to build jobs-read.db at")
    ap.add_argument("--key", default="jobs-read.db", help="S3 key to push the merged DB to (default: jobs-read.db)")
    ap.add_argument("--prefix", default=PARTITION_PREFIX,
                     help=f"S3 key prefix identifying a partition file (default: {PARTITION_PREFIX})")
    ap.add_argument("--tmp-dir", type=Path, default=Path("/tmp"), help="scratch dir for downloaded partitions")
    args = ap.parse_args()

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

    # Best-effort: no names file just means every company falls back to
    # its domain, exactly as before this existed.
    names = None
    if args.bucket:
        names_path = args.out.with_name("company-names.json")
        try:
            existed, _ = s3_pull(args.bucket, "company-names.json", names_path)
            if existed:
                names = json.loads(names_path.read_text(encoding="utf-8"))
                print(f"loaded {len(names)} company names", file=sys.stderr)
        except Exception as e:
            print(f"couldn't load company-names.json (non-fatal): {e!r}", file=sys.stderr)

    summary = merge_partitions(partition_paths, args.out, names)
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
