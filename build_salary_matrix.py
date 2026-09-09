"""Rebuild salary-matrix.json from every disclosed range in the snapshot.

The estimator (salary_model.py) is a table of medians over cells of real
disclosed pay. This is the step that computes those cells and publishes
them, so the scrape Lambda can look one up in memory instead of carrying
a hand-transcribed table around.

Daily, not per-merge. The cells move at the speed employers post jobs,
which is slow, and rebuilding them on the 5-minute apply would put a full
table scan of the snapshot in the path of the thing that has to stay
fast. A day-old median is indistinguishable from a fresh one.

Nothing here touches the live database. It reads the snapshot and writes
one small JSON object beside it, the same shape resolve_company_names.py
already uses.

    python build_salary_matrix.py --bucket $DATA_BUCKET
    python build_salary_matrix.py --db jobs-read.db --out matrix.json --dry-run
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from salary_model import MIN_ROWS, SalaryModel, parse_disclosed  # noqa: E402

KEY = "salary-matrix.json"

# One model per currency, because a cell mixing dollars and pounds is
# not a market. Currencies below this many disclosed rows are dropped
# rather than published: a handful of rows cannot fill even the broadest
# cell in the chain, so publishing them only adds weight to a file the
# scrape Lambda downloads on every cold start.
MIN_ROWS_PER_CURRENCY = 200


def load_rows(db_path: Path) -> list[tuple[dict, float, str]]:
    """(job, annual pay, currency) for every listing with a real range.

    Estimates are excluded by definition. Training a model on its own
    output is how a table of medians turns into a table of nothing.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """
            SELECT id, company_domain, location, seniority, department, salary_text
            FROM jobs
            WHERE salary_text IS NOT NULL AND salary_text != ''
              AND salary_is_estimate = 0
              AND closed_at IS NULL
            """
        )
        rows = []
        for row in cursor:
            parsed = parse_disclosed(row["salary_text"])
            if not parsed:
                continue
            currency, low, high = parsed
            rows.append((dict(row), (low + high) / 2, currency))
        return rows
    finally:
        conn.close()


def dedupe(rows):
    """One row per company and exact figure.

    A company posting the same range across eight near-identical reqs
    would otherwise weight that figure eight times, which is a fact about
    their hiring plan rather than about the market.
    """
    seen, out = set(), []
    for job, pay, currency in rows:
        key = (job["company_domain"], currency, round(pay))
        if key in seen:
            continue
        seen.add(key)
        out.append((job, pay, currency))
    return out


def build(rows) -> dict:
    by_currency = {}
    for job, pay, currency in rows:
        by_currency.setdefault(currency, []).append((job, pay))
    matrix = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "models": {}}
    for currency, pairs in sorted(by_currency.items(), key=lambda kv: -len(kv[1])):
        if len(pairs) < MIN_ROWS_PER_CURRENCY:
            print(f"  {currency}: {len(pairs)} rows, below {MIN_ROWS_PER_CURRENCY}, skipped",
                  file=sys.stderr)
            continue
        model = SalaryModel.build(pairs, currency)
        filled = sum(len(group) for level, group in model.cells.items() if level != "global")
        print(f"  {currency}: {len(pairs):,} rows, {filled:,} cells with at least {MIN_ROWS}",
              file=sys.stderr)
        matrix["models"][currency] = json.loads(model.to_json())
    return matrix


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("jobs-read.db"),
                    help="local snapshot to read; pulled from --bucket when absent")
    ap.add_argument("--bucket", help="S3 bucket to pull the snapshot from and publish the matrix to")
    ap.add_argument("--snapshot-key", default="jobs-read.db")
    ap.add_argument("--out", type=Path, help="also write the matrix here")
    ap.add_argument("--dry-run", action="store_true", help="build and report, publish nothing")
    args = ap.parse_args()

    if args.bucket and not args.db.exists():
        import boto3

        print(f"pulling s3://{args.bucket}/{args.snapshot_key}...", file=sys.stderr)
        boto3.client("s3").download_file(args.bucket, args.snapshot_key, str(args.db))
    if not args.db.exists():
        print(f"no snapshot at {args.db} and no --bucket to pull one from", file=sys.stderr)
        return 1

    started = time.time()
    rows = load_rows(args.db)
    deduped = dedupe(rows)
    print(f"{len(rows):,} disclosed ranges parsed, {len(deduped):,} after de-duplication",
          file=sys.stderr)
    matrix = build(deduped)
    if not matrix["models"]:
        print("no currency had enough rows; refusing to publish an empty matrix", file=sys.stderr)
        return 1

    body = json.dumps(matrix, separators=(",", ":")).encode("utf-8")
    print(f"matrix is {len(body) / 1024:.0f} KB, built in {time.time() - started:.0f}s",
          file=sys.stderr)
    if args.out:
        args.out.write_bytes(body)
        print(f"wrote {args.out}", file=sys.stderr)
    if args.dry_run or not args.bucket:
        return 0

    import boto3

    boto3.client("s3").put_object(
        Bucket=args.bucket, Key=KEY, Body=body, ContentType="application/json"
    )
    print(f"published s3://{args.bucket}/{KEY}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
