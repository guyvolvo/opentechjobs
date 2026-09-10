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
from datetime import datetime, timezone
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

# How stale these are allowed to get before a run recomputes them.
#
# This is the whole cost control. Measured in the applier: building both
# artifacts takes 26.7 seconds, against a snapshot push that finishes in
# under one. Running it on every five-minute apply made it three
# quarters of the applier's entire duration.
#
# Nothing here needs five-minute resolution. The panels show 14-day
# trends, headline counts in the tens of thousands, and facet counts
# used to populate dropdowns. Fifteen minutes is invisible in all three,
# and it turns 288 builds a day into 96.
MAX_AGE_S = 900


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
        # Keyed by confidence, because that is the one filter the page
        # always sends and never leaves empty. The board defaults to
        # "all" (verified plus best-effort, shown with a badge), while
        # the API defaults to verified only. Precomputing just one of
        # those meant the artifact existed and the page never used it:
        # /facets?x=1 answered in 0.34s while the request the browser
        # actually makes still took 7.18s.
        facets = {c: compute_facets(conn, {"confidence": c}) for c in ("verified", "all")}
    finally:
        conn.close()
    return {"stats.json": stats, "facets.json": facets}


def _fresh_enough(s3, bucket: str) -> bool:
    """Whether the existing artifacts are recent enough to leave alone.

    One HEAD against the object we would overwrite. Missing or
    unreadable means build it, which is the right answer for a first run
    and for anything that has gone wrong.
    """
    try:
        head = s3.head_object(Bucket=bucket, Key=f"{PREFIX}stats.json")
    except Exception:
        return False
    age = (datetime.now(timezone.utc) - head["LastModified"]).total_seconds()
    if age < MAX_AGE_S:
        print(f"precomputed artifacts are {age:.0f}s old, leaving them", file=sys.stderr)
        return True
    return False


def publish(bucket: str, db_path: Path, frontend_bucket: str = "") -> list[str]:
    """Write them to S3. Returns the keys written, [] on any failure.

    Two destinations, for two different readers.

    The data bucket copy is what /api/stats and /api/facets serve, so
    anyone calling the API keeps getting the same answer from the same
    place. The frontend bucket copy is fetched straight from CloudFront
    by the browser, which is where the saving is: the page polls these
    every two minutes per open tab, and every one of those was invoking
    a Lambda to hand back bytes that were already sitting in S3.

    Same trick bootstrap.json has used all along, and the same posture:
    best effort, because a merge that has already pushed a snapshot must
    not fail over a dashboard.
    """
    if not bucket:
        return []
    import boto3

    s3_probe = boto3.client("s3")
    if _fresh_enough(s3_probe, bucket):
        return []

    try:
        payloads = build(db_path)
    except Exception as e:
        print(f"precompute failed, API will keep computing live: {e!r}", file=sys.stderr)
        return []

    s3 = s3_probe
    written = []
    for name, payload in payloads.items():
        body = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        for target, key in ((bucket, f"{PREFIX}{name}"),
                            (frontend_bucket, name) if frontend_bucket else (None, None)):
            if not target:
                continue
            try:
                # 60s max-age: the browser holds it for less than half a
                # write cycle, so a reload inside that window costs no
                # network at all and still cannot show a stale figure for
                # longer than the data takes to change.
                s3.put_object(
                    Bucket=target, Key=key, Body=body,
                    ContentType="application/json",
                    CacheControl="public, max-age=60",
                )
                written.append(f"{target}/{key}")
            except Exception as e:
                print(f"couldn't write {key} to {target} (non-fatal): {e!r}", file=sys.stderr)
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
