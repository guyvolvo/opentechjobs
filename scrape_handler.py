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
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).parent
TMP = Path("/tmp")
BUCKET = os.environ["DATA_BUCKET"]


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

    probe = subprocess.run(
        [sys.executable, str(ROOT / "probe.py"), "--known", str(known_path), "--json"],
        capture_output=True, text=True, timeout=90,
    )
    if probe.stderr:
        print(probe.stderr)
    if probe.returncode != 0:
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

    load = subprocess.run(
        [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
         "--resolved", str(resolved_path), "--out", str(TMP / "jobs.db"),
         "--bucket", BUCKET, "--key", "jobs.db"],
        capture_output=True, text=True, timeout=25,
    )
    # load_to_sqlite.py logs its own progress to stderr, not stdout.
    if load.stderr:
        print(load.stderr)
    if load.returncode != 0:
        raise RuntimeError(f"load_to_sqlite.py exited {load.returncode}")

    return {"hits": len(hits), "errors": len(errors), "jobs": n_jobs}
