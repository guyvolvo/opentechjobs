"""EventBridge-triggered Lambda for the Partition & Merge design's merge
step -- combines every writer's own small jobs-partition-{name}.db back
into jobs-read.db, the one file api/db.py actually reads.

Started life (2026-09-08, earlier the same day) as a once-daily VACUUM-only
pass, split out of the frequent re-poll cycles after VACUUM's own
whole-file-rewrite cost was found to scale with jobs.db's TOTAL size, not
with how much any given run touched. Became this, the same day, once it
was clear sharding alone hadn't fixed the real problem: scrape_handler.py
and scrape_workday_handler.py's own pull-modify-push cycles were STILL
downloading and uploading the FULL jobs.db every invocation regardless of
shard size, which is what actually caused that day's Runtime.OutOfMemory
outage. Partition & Merge's fix: each writer now touches only its own
small partition file (see scrape_handler.py's own docstring), and this
Lambda is the once-an-hour step that turns those back into one coherent
read snapshot -- see loader/merge_partitions.py's own docstring for the
merge logic itself, including how a company's stale row in a partition it
no longer belongs to gets dropped instead of merged.

Runs hourly, not once a day: jobs-read.db only gets fresher when this
runs, so its cadence is now the real ceiling on how stale the live site's
listings can be (see scrape_maintenance_lambda.tf's own schedule
comment), not a free-standing maintenance detail the way daily VACUUM
was.

Writes its own merge-status.json (2026-09-08, requested directly: the
site's own "API Status" card had no way to show a merge actually in
progress, or the source that its "next sync" countdown could honestly
anchor on beyond a guess). A SEPARATE key from status.json, not a shared
one -- that file is fast-poll/workday's own, on a 5-10 minute cadence;
sharing it would mean this Lambda's own "merging" phase gets overwritten
within seconds by the next fast-poll write, same problem route_
pipeline_status's own staleness check exists to catch for THAT file, just
guaranteed to happen on nearly every real merge instead of only on a
crash.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "loader"))

from alerts import evaluate_alerts  # noqa: E402
from deltas import delete_fragments, list_fragments, read_fragments  # noqa: E402
TMP = Path("/tmp")
BUCKET = os.environ["DATA_BUCKET"]
# Where bootstrap.json goes. Optional: unset just means the site keeps
# fetching its first page from the API, which is what it did before.
FRONTEND_BUCKET = os.environ.get("FRONTEND_BUCKET")


def _write_status(s3, phase: str, detail: str = "") -> None:
    """Same shape as scrape_handler.py's own _write_status, own key
    (merge-status.json) -- see this module's own docstring for why.
    """
    try:
        s3.put_object(
            Bucket=BUCKET, Key="merge-status.json", ContentType="application/json",
            Body=json.dumps({
                "phase": phase, "detail": detail,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }).encode("utf-8"),
        )
    except Exception as e:
        print(f"merge-status.json write failed (non-fatal): {e!r}")


def _publish_bootstrap(s3) -> None:
    """Publish the default first page as a static file for CloudFront.

    Runs here rather than anywhere else because this is the only moment
    the data changes, and because the freshly-merged snapshot is already
    sitting on local disk. Best-effort throughout: a failure costs the
    site its fast first paint, never its correctness, since the frontend
    falls back to the normal API fetch whenever this file is missing,
    stale-shaped, or simply doesn't match the query it was about to make.
    """
    if not FRONTEND_BUCKET:
        return
    out = TMP / "bootstrap.json"
    try:
        build = subprocess.run(
            [sys.executable, str(ROOT / "loader" / "bootstrap.py"),
             "--db", str(TMP / "jobs-read.db"), "--out", str(out)],
            capture_output=True, text=True, timeout=60,
        )
        if build.stderr:
            print(build.stderr.strip())
        if build.returncode != 0:
            print(f"bootstrap build failed (non-fatal): exit {build.returncode}")
            return
        s3.put_object(
            Bucket=FRONTEND_BUCKET, Key="bootstrap.json",
            Body=out.read_bytes(), ContentType="application/json",
            # Short browser TTL, longer at the edge, and a generous
            # stale-while-revalidate so a visitor never waits on a
            # revalidation round trip. All of it is bounded by the merge
            # cadence anyway: this content is only ever regenerated here.
            CacheControl="public, max-age=60, s-maxage=300, stale-while-revalidate=600",
        )
        print(f"published bootstrap.json ({out.stat().st_size} bytes) to {FRONTEND_BUCKET}")
    except Exception as e:
        print(f"bootstrap publish failed (non-fatal, site falls back to the API): {e!r}")


def lambda_handler(event, context):
    """Apply pending delta fragments to the live snapshot.

    Was a merge of every jobs-partition-*.db into a fresh jobs-read.db.
    That made sense while each writer owned a partition, but the sweep no
    longer writes partitions at all: persisting one cost a 48MB
    pull-modify-push per shard touched, which is what capped how wide a
    sweep could be. Writers now emit small delta fragments and this
    replays them.

    Incremental, not a rebuild. Pulling the 284MB snapshot, applying a
    few kilobytes and pushing it back is far less work than downloading
    every partition, and it is what lets this run every 5 minutes instead
    of hourly.

    Fragments are deleted only after the new snapshot is safely pushed,
    so a failure anywhere in here costs a repeat rather than a listing.
    """
    s3 = boto3.client("s3")
    keys = list_fragments(BUCKET)
    if not keys:
        _write_status(s3, "idle", "no pending deltas")
        print("no pending delta fragments")
        return {"ok": True, "applied": 0, "fragments": 0}

    _write_status(s3, "merging", f"applying {len(keys)} delta fragments to jobs-read.db")
    results = read_fragments(BUCKET, keys)
    if not results:
        # Every fragment unreadable. Leave them alone rather than delete
        # what was never applied.
        _write_status(s3, "error", "delta fragments present but none readable")
        raise RuntimeError("delta fragments present but none readable")

    resolved = TMP / "delta-resolved.json"
    resolved.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    snapshot = TMP / "jobs-read.db"

    load = subprocess.run(
        [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
         "--resolved", str(resolved), "--out", str(snapshot),
         "--bucket", BUCKET, "--key", "jobs-read.db",
         "--drop-description", "--skip-vacuum", "--skip-known"],
        capture_output=True, text=True, timeout=550,
    )
    if load.stderr:
        print(load.stderr)
    if load.returncode != 0:
        _write_status(s3, "error", f"load_to_sqlite.py exited {load.returncode}")
        raise RuntimeError(f"load_to_sqlite.py exited {load.returncode}")

    # Only now: the snapshot carrying them is pushed.
    removed = delete_fragments(BUCKET, keys)
    print(f"applied {len(results)} company results from {len(keys)} fragments, "
          f"{removed} fragments cleared")

    # Alerts moved here from the sweep, which no longer opens a database.
    # Better placed anyway: one evaluation against the full snapshot
    # rather than one per partition, right after the data lands.
    _write_status(s3, "sending alerts", "matching new listings against saved filters")
    alerts_result = evaluate_alerts(snapshot)
    if alerts_result.get("errors"):
        print(f"alert evaluation errors: {alerts_result['errors']}")

    summary = {"applied": len(results), "fragments": len(keys), "alerts": alerts_result}
    _publish_bootstrap(s3)

    print(f"delta apply complete: {json.dumps(summary, default=str)}")
    _write_status(s3, "idle", f"last apply: {len(results)} companies from {len(keys)} fragments")
    return {"ok": True, **summary}
