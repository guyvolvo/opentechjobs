"""Answer the two expensive aggregate routes once per merge, not per request.

Measured with the edge cache bypassed, /api/stats took 7.03s and
/api/facets 5.02s, against 0.44s for /api/jobs. Both fire on every page
load. Between them they were most of the API's compute bill, and none of
it was work that differs between one visitor and the next: the same
snapshot produces the same answer until the next merge replaces it.

So the applier computes them against the snapshot it just built and
writes the results next to it. The API reads a finished number.

What is NOT precomputed here is anything filtered. Facets are counted
with the caller's other active filters applied, so only the unfiltered
case has a single answer, and handler.py falls back to computing the
rest live. Stats vary only by israel_only, and only in one field, so
both versions of that field ship in the same object.

Writing these is best effort by design. A failure here must not fail a
merge or hold up a snapshot: the API treats a missing file as "compute
it yourself", which is also what it does in the window between deploying
that code and this ever running.
"""

import json
import sqlite3
import sys
from pathlib import Path

# Two layouts to satisfy. In the repo these live under api/; in the
# deployed package the workflow flattens them next to the handler, one
# level above this file. Both go on the path so the same import works
# from a checkout and from a Lambda.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "api"))

from aggregates import compute_facets, compute_stats  # noqa: E402
from job_filters import register_functions  # noqa: E402

PREFIX = "precomputed/"


def build(db_path: Path) -> dict[str, dict]:
    """{filename: payload} for everything worth precomputing."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    register_functions(conn)
    try:
        stats = compute_stats(conn, {})
        # The one field israel_only changes. Computed from the same
        # connection rather than a second pass over the whole route.
        stats["top_locations_israel"] = compute_stats(
            conn, {"israel_only": "1"})["top_locations"]
        facets = compute_facets(conn, {})
    finally:
        conn.close()
    return {"stats.json": stats, "facets.json": facets}


def publish(bucket: str, db_path: Path) -> list[str]:
    """Write them to S3. Returns the keys written, [] on any failure.

    Never raises. The caller is a merge that has already pushed a
    snapshot, and a slow dashboard is not worth failing that over.
    """
    if not bucket:
        return []
    import boto3

    try:
        payloads = build(db_path)
    except Exception as e:
        print(f"precompute failed, API will keep computing live: {e!r}", file=sys.stderr)
        return []

    s3 = boto3.client("s3")
    written = []
    for name, payload in payloads.items():
        try:
            s3.put_object(
                Bucket=bucket, Key=f"{PREFIX}{name}",
                Body=json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json",
            )
            written.append(f"{PREFIX}{name}")
        except Exception as e:
            print(f"couldn't write {PREFIX}{name} (non-fatal): {e!r}", file=sys.stderr)
    return written


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--bucket")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.dry_run or not args.bucket:
        for name, payload in build(args.db).items():
            body = json.dumps(payload, default=str, ensure_ascii=False)
            print(f"{name}: {len(body):,} bytes, {len(payload)} top-level keys")
    else:
        print("\n".join(publish(args.bucket, args.db)) or "nothing written")
