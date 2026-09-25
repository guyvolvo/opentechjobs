"""Stamp resolved company names onto the box's own database.

Why this exists. Names are resolved once, by resolve_company_names.py,
into company-names.json in S3, and used to reach the site through the
Lambda merge (loader/merge_partitions.py's apply_company_names). The
box replaced that merge and nothing on it ever read the file: measured
2026-09-25, the file held 8,824 names and the box showed 2,915, the
ones frozen into the seed at cutover. Everything resolved since had
gone nowhere, and the board captioned 89% of open listings with a bare
domain.

The file wins. A name in it replaces whatever the row has, and a domain
the resolver tried and got nothing for ("" in the file) clears the row,
so a name retracted after the cleaning rules tightened comes off the
site too. The first version was fill-only, which could add a name but
never take one back, and Northrop Grumman sat captioned "Northwest
Gospel Church" for it. A company missing from the file altogether keeps
whatever it has. The applier's own company upsert lists the columns it
updates on conflict and company_name is not one of them, so a name
written here survives every later delta too, which is what makes this a
pair of UPDATEs rather than a change to the applier.

Takes the same flock the applier and the snapshot take, and leaves if
either holds it: 20k UPDATEs on the primary key take under a second and
are not worth queueing behind a 2.5GB VACUUM.

    python box/apply_company_names.py            # from S3 and the local copy, plus STATIC_NAMES
    python box/apply_company_names.py --dry-run  # count, write nothing
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lock import exclusive  # noqa: E402
from resolve_company_names import NAMES_KEY, STATIC_NAMES  # noqa: E402

DB = Path(os.environ.get("DATA_PATH", "/var/lib/otj/jobs.db"))
BUCKET = os.environ.get("DATA_BUCKET", "")


def load_names(bucket: str) -> dict[str, str]:
    names: dict[str, str] = {}
    if bucket:
        import boto3
        try:
            body = boto3.client("s3").get_object(Bucket=bucket, Key=NAMES_KEY)["Body"].read()
            names.update(json.loads(body))
        except Exception as e:  # noqa: BLE001 -- a missing file is "nothing resolved yet", not a crash
            print(f"no {NAMES_KEY} in s3://{bucket}: {e}", file=sys.stderr)
    # The resolver's own copy beside the database, on top of S3's: it is
    # written before the bucket is, so it is never behind.
    local = DB.with_name(NAMES_KEY)
    if local.exists():
        try:
            names.update(json.loads(local.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"ignoring unreadable {local}: {e}", file=sys.stderr)
    # The static map wins: it is the one place a wrong resolved name can
    # be corrected without touching S3.
    names.update(STATIC_NAMES)
    return {d: (n or "").strip() for d, n in names.items()}


def coverage(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        companies, named = conn.execute(
            "SELECT count(*), sum(company_name IS NOT NULL AND company_name <> '') FROM companies"
        ).fetchone()
        open_jobs, open_named = conn.execute(
            "SELECT count(*), sum(c.company_name IS NOT NULL AND c.company_name <> '') "
            "FROM jobs j LEFT JOIN companies c ON c.domain = j.company_domain WHERE j.closed_at IS NULL"
        ).fetchone()
    finally:
        conn.close()
    return {"companies": companies, "named": named or 0, "open_jobs": open_jobs, "open_named": open_named or 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    names = load_names(BUCKET)
    rows = [(n, d, n) for d, n in names.items() if n]
    empties = [(d,) for d, n in names.items() if not n]
    before = coverage(DB)
    print(f"{len(rows)} names and {len(empties)} tried-empty on hand; before: "
          f"{before['named']}/{before['companies']} companies, "
          f"{before['open_named']}/{before['open_jobs']} open jobs named", file=sys.stderr)
    if args.dry_run:
        return 0

    with exclusive("apply-company-names"):
        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            with conn:
                conn.executemany(
                    "UPDATE companies SET company_name = ? "
                    "WHERE domain = ? AND (company_name IS NULL OR company_name <> ?)",
                    rows,
                )
                written = conn.total_changes
                conn.executemany(
                    "UPDATE companies SET company_name = NULL "
                    "WHERE domain = ? AND company_name IS NOT NULL",
                    empties,
                )
                cleared = conn.total_changes - written
        finally:
            conn.close()

    after = coverage(DB)
    print(f"wrote {written}, cleared {cleared}; after: {after['named']}/{after['companies']} companies, "
          f"{after['open_named']}/{after['open_jobs']} open jobs named", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
