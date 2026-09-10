"""Retire closed listings from the served snapshot into cold storage.

Nothing ever deleted a job, so the snapshot only grows. Measured
2026-09-10, nine days into this corpus: 115k open listings and 27k
closed, with roughly 3,140 closing a day and open holding flat. That
curve reaches 398k rows and 1.05GB by December and 1.25M rows and 3.29GB
within a year, at which point nine rows in ten are jobs nobody can apply
to. The board filters every one of them out on every query, and the
applier still rewrites and reships all of them every five minutes.

Worse than the cost, there is a wall: the API downloads the whole
snapshot on a cold start, and API Gateway's integration timeout is a
fixed 29 seconds that no setting raises.

So closed listings older than RETAIN_DAYS leave the snapshot. Everything
that reads a closed job reads a recent one: the 24h and 7d throughput
counters, time-to-fill, and the 14-day reconstructed history. Thirty
days is comfortably past all of them.

They are written to S3 first and deleted only after that write returns,
one immutable object per run under the month they closed in. Appending
to an object is not a thing S3 does, and read-modify-write on a growing
archive would recreate the exact problem this is here to solve.
"""

import gzip
import json
import sys
from datetime import datetime, timezone

PREFIX = "archive/closed/"
MARKER_KEY = "archive/last-prune.json"

# Well past the 14-day window every consumer of a closed job uses.
RETAIN_DAYS = 30

# How often this is worth doing. Only ~3,000 listings cross the
# threshold on any given day, so running it every 5 minutes would write
# 288 near-empty objects a day to save a few hundred rows. Once a day
# keeps the snapshot capped just as effectively.
MIN_HOURS_BETWEEN_RUNS = 20

# Columns worth keeping. Everything except the description, which
# already lives as its own S3 object and is the reason the snapshot is
# not four times this size.
_COLUMNS = """id, company_domain, ats, external_id, title, location, department, url,
              posted_at, description_chars, seniority, workplace_type, skills,
              salary_text, salary_is_estimate, salary_source, confidence,
              first_seen, last_seen, closed_at"""


def due(s3, bucket: str) -> bool:
    """Whether enough time has passed since the last run.

    A missing or unreadable marker means yes. The work is idempotent and
    doing it a second time costs one small object, so erring towards
    running is the cheap direction to be wrong in.
    """
    try:
        body = s3.get_object(Bucket=bucket, Key=MARKER_KEY)["Body"].read()
        last = datetime.fromisoformat(json.loads(body)["ran_at"])
    except Exception:
        return True
    hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return hours >= MIN_HOURS_BETWEEN_RUNS


def prune(conn, s3, bucket: str, retain_days: int = RETAIN_DAYS,
          fts_rowid_delete: bool = True) -> dict:
    """Archive and remove listings closed longer than retain_days ago.

    Returns a summary. Raises nothing the caller has to handle: on any
    failure the rows stay exactly where they are, which is the safe
    direction. A snapshot carrying too much history is a cost problem; a
    snapshot missing listings is a correctness one.
    """
    rows = conn.execute(
        f"""SELECT rowid, {_COLUMNS} FROM jobs
            WHERE closed_at IS NOT NULL
              AND julianday('now') - julianday(closed_at) > ?""",
        (retain_days,),
    ).fetchall()
    if not rows:
        return {"archived": 0, "objects": 0}

    # Grouped by the month a listing closed in, so the archive is
    # browsable and a reader can fetch one period without scanning all.
    by_month: dict[str, list[dict]] = {}
    for r in rows:
        record = {k: r[k] for k in r.keys() if k != "rowid"}
        by_month.setdefault((record["closed_at"] or "")[:7] or "unknown", []).append(record)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written = []
    try:
        for month, records in sorted(by_month.items()):
            body = "\n".join(json.dumps(r, default=str, ensure_ascii=False)
                             for r in records).encode("utf-8")
            key = f"{PREFIX}{month}/{stamp}.jsonl.gz"
            s3.put_object(Bucket=bucket, Key=key, Body=gzip.compress(body),
                          ContentType="application/gzip")
            written.append(key)
    except Exception as e:
        # Nothing is deleted. A partial archive just means some rows get
        # written again next run, and re-archiving is harmless because
        # each object is named for the run that produced it.
        print(f"archive write failed, keeping every row in the snapshot: {e!r}", file=sys.stderr)
        return {"archived": 0, "objects": len(written), "error": repr(e)}

    ids = [r["rowid"] for r in rows]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        if fts_rowid_delete:
            # Contentless FTS can only drop a row by rowid, and only on
            # 3.43+. Where it cannot, the terms stay and the listing
            # remains matchable by keyword after its row is gone, which
            # is why this is a flag rather than an assumption.
            conn.execute(f"DELETE FROM jobs_fts WHERE rowid IN ({marks})", chunk)
        conn.execute(f"DELETE FROM jobs WHERE rowid IN ({marks})", chunk)

    try:
        s3.put_object(
            Bucket=bucket, Key=MARKER_KEY,
            Body=json.dumps({"ran_at": datetime.now(timezone.utc).isoformat(),
                             "archived": len(rows), "objects": written}).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:
        # The marker only paces this. Losing it means running again
        # sooner than needed, which finds nothing and writes nothing.
        print(f"couldn't write the prune marker (non-fatal): {e!r}", file=sys.stderr)

    return {"archived": len(rows), "objects": len(written), "months": sorted(by_month)}
