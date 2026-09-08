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
TMP = Path("/tmp")
BUCKET = os.environ["DATA_BUCKET"]


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


def lambda_handler(event, context):
    s3 = boto3.client("s3")
    _write_status(s3, "merging", "combining every writer's own partition into jobs-read.db")

    merge = subprocess.run(
        [sys.executable, str(ROOT / "loader" / "merge_partitions.py"),
         "--bucket", BUCKET, "--out", str(TMP / "jobs-read.db"), "--key", "jobs-read.db"],
        capture_output=True, text=True, timeout=550,
    )
    # merge_partitions.py logs its own progress (including the per-run
    # summary dict and any schema-mismatch alert) to stderr, not stdout.
    if merge.stderr:
        print(merge.stderr)
    if merge.returncode != 0:
        _write_status(s3, "error", f"merge_partitions.py exited {merge.returncode}")
        raise RuntimeError(f"merge_partitions.py exited {merge.returncode}")

    # The summary dict is the last stderr line merge_partitions.py prints
    # -- surfaced in this Lambda's own return value so a manual invoke
    # (or a CloudWatch Logs Insights query) can see it without digging
    # through the full log stream.
    summary = {}
    for line in reversed(merge.stderr.splitlines()):
        try:
            summary = json.loads(line)
            break
        except json.JSONDecodeError:
            continue

    print(f"merge complete: {json.dumps(summary)}")
    _write_status(s3, "idle", f"last merge: {summary.get('companies_merged', '?')} companies "
                              f"across {len(summary.get('partitions', []))} partitions")
    return {"ok": True, "summary": summary}
