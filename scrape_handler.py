"""EventBridge-triggered Lambda for the fast re-poll cycle.

GitHub Actions' schedule: trigger turned out to not be reliable enough to
depend on: scrape-fast.yml's cron sat for over an hour, and every offset
tried, without firing a single scheduled run (only manual dispatches ever
ran). That's a known, long-standing GitHub issue with no official fix --
see https://github.com/orgs/community/discussions/147369. EventBridge has
an actual SLA, so this Lambda now owns the recurring cadence entirely;
scrape-fast.yml keeps workflow_dispatch only, for manual/on-demand runs.

Runs probe.py --known and loader/load_to_sqlite.py as subprocesses against
/tmp, exactly the same two commands scrape-fast.yml already ran -- reusing
those already-proven CLI entry points rather than re-implementing their
logic here.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from alerts import evaluate_alerts

ROOT = Path(__file__).parent
TMP = Path("/tmp")
BUCKET = os.environ["DATA_BUCKET"]


def _write_status(s3, phase: str, detail: str = "") -> None:
    """Best-effort, real-time "what is the pipeline doing right now"
    signal -- read by /api/pipeline-status (api/handler.py) so the
    frontend can show an actual phase (scraping/loading/idle/error)
    instead of just a last-updated timestamp, which says nothing about
    whether a run is even in progress. Never allowed to break the real
    pipeline: a status write failing is a cosmetic loss, not a reason to
    fail the whole invocation.
    """
    try:
        s3.put_object(
            Bucket=BUCKET, Key="status.json",
            Body=json.dumps({
                "phase": phase, "detail": detail, "run": "fast-poll",
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:
        print(f"status.json write failed (non-fatal): {e!r}")


def lambda_handler(event, context):
    s3 = boto3.client("s3")
    known_path = TMP / "known.json"
    try:
        s3.download_file(BUCKET, "known.json", str(known_path))
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            # Same as scrape-fast.yml's own early exit: nothing to
            # re-poll before scrape-discover.yml's first run has ever
            # produced known.json.
            print("known.json not in S3 yet, skipping this run")
            return {"skipped": True}
        raise

    known_count = len(json.loads(known_path.read_text(encoding="utf-8")))
    _write_status(s3, "scraping", f"re-checking {known_count} known companies")

    probe = subprocess.run(
        [sys.executable, str(ROOT / "probe.py"), "--known", str(known_path), "--json"],
        capture_output=True, text=True, timeout=90,
    )
    if probe.stderr:
        print(probe.stderr)
    if probe.returncode != 0:
        _write_status(s3, "error", f"probe.py exited {probe.returncode}")
        raise RuntimeError(f"probe.py exited {probe.returncode}")

    resolved_path = TMP / "resolved.json"
    resolved_path.write_text(probe.stdout, encoding="utf-8")

    data = json.loads(probe.stdout)
    hits = [r for r in data if r.get("ats")]
    errors = [r["domain"] for r in data if r.get("error")]
    n_jobs = sum(r["job_count"] for r in hits)
    print(f"{len(hits)}/{len(data)} re-verified, {n_jobs} jobs")
    if errors:
        print(f"{len(errors)} known boards failed to re-poll: {errors}")

    # 60, not the original 25: load_to_sqlite.py now retries its whole
    # pull-modify-push cycle on a conflicting concurrent write (see its
    # own s3_push_conditional docstring) instead of one writer silently
    # clobbering the other -- each full cycle measured ~6s in practice,
    # so 25s left room for barely more than one retry. A
    # subprocess.TimeoutExpired here isn't caught anywhere below --
    # exactly the failure mode that caused the original Workday outage
    # (see WORKDAY_MAX_JOBS's own comment in probe.py) -- so this needs
    # real headroom for a legitimate retry, not just the happy path.
    _write_status(s3, "loading", f"writing {n_jobs} jobs to jobs.db")
    load = subprocess.run(
        [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
         "--resolved", str(resolved_path), "--out", str(TMP / "jobs.db"),
         "--bucket", BUCKET, "--key", "jobs.db"],
        capture_output=True, text=True, timeout=60,
    )
    # load_to_sqlite.py logs its own progress to stderr, not stdout.
    if load.stderr:
        print(load.stderr)
    if load.returncode != 0:
        _write_status(s3, "error", f"load_to_sqlite.py exited {load.returncode}")
        raise RuntimeError(f"load_to_sqlite.py exited {load.returncode}")

    # jobs.db is already fresh on /tmp from the loader step just above --
    # evaluate_alerts() reads it directly, no separate download. Alert
    # failures are caught and reported inside evaluate_alerts() itself
    # (one bad alert shouldn't stop the others), so nothing here needs to
    # guard the fast-poll's own success on this step succeeding.
    _write_status(s3, "sending alerts", "matching new listings against saved filters")
    alerts_result = evaluate_alerts(TMP / "jobs.db")
    if alerts_result.get("errors"):
        print(f"alert evaluation errors: {alerts_result['errors']}")

    _write_status(s3, "idle", f"last run: {len(hits)}/{len(hits) + len(errors)} companies, {n_jobs} jobs")
    return {"hits": len(hits), "errors": len(errors), "jobs": n_jobs, "alerts": alerts_result}
