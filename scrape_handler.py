"""EventBridge-triggered Lambda for the fast re-poll cycle.

GitHub Actions' schedule: trigger turned out to not be reliable enough to
depend on: scrape-fast.yml's cron sat for over an hour, and every offset
tried, without firing a single scheduled run (only manual dispatches ever
ran). That's a known, long-standing GitHub issue with no official fix --
see https://github.com/orgs/community/discussions/147369. EventBridge has
an actual SLA, so this Lambda now owns the recurring cadence entirely;
scrape-fast.yml keeps workflow_dispatch only, for manual/on-demand runs.

Sharded, not a full re-poll every cycle. Reported live (2026-09-08): once
company discovery became a standing, continuously-refilling pipeline
(discover-companies.yml + merge-discovered-companies.yml) instead of a
one-time batch, re-polling the ENTIRE known.json every 5 minutes meant
this Lambda's cost grows forever, linearly, with total company count --
a real run at 358 companies was already projected past $30/month on its
own before the queue even finished draining. There's no ceiling on how
many companies discovery eventually finds, so a design whose cost scales
with that number can't have a stable budget.

Splitting known.json into fixed-size shards and only re-polling ONE
shard per invocation decouples cost from total company count: shard size
stays constant as the company list grows, so per-invocation cost (and
the monthly total, at a fixed schedule) stays roughly flat too. What
grows instead is the full-rotation latency (every company gets re-polled
once per NUM_SHARDS invocations) -- a graceful degradation instead of a
runaway bill. Which shard runs is computed from wall-clock time, not
carried in the EventBridge event, so no scheduler config needs to change
as the company count (and therefore NUM_SHARDS) grows.

VACUUM is NOT run here -- see load_to_sqlite.py's own --skip-vacuum
docstring for why: it rewrites the whole DB file regardless of shard
size, which would silently reintroduce the exact per-company-count cost
scaling this sharding exists to remove. scrape_maintenance_handler.py's
own daily run owns VACUUM now.

Runs probe.py --known and loader/load_to_sqlite.py as subprocesses
against /tmp, exactly the same two commands scrape-fast.yml already ran
-- reusing those already-proven CLI entry points rather than
re-implementing their logic here.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from alerts import evaluate_alerts

ROOT = Path(__file__).parent
TMP = Path("/tmp")
BUCKET = os.environ["DATA_BUCKET"]

# ~50 companies/shard measured comfortably under a minute per invocation
# with real network I/O to each board's own API -- see this module's own
# docstring for why a fixed shard size (not a fixed shard COUNT) is what
# keeps per-invocation cost flat as the company list grows.
SHARD_SIZE = 50

# Matches the EventBridge schedule below (rate(5 minutes)) -- used only
# to pick a shard from wall-clock time, not to enforce timing itself.
SCHEDULE_INTERVAL_S = 300


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


def _pick_shard(known: list) -> tuple[list, int, int]:
    """Deterministic partition (sorted by domain, sliced into fixed-size
    chunks) so which companies land in which shard doesn't shuffle
    between invocations just because dict/JSON ordering changed -- only
    NUM_SHARDS growing (as known.json grows) should ever move a company
    to a different shard. Which shard runs THIS invocation comes from
    wall-clock time, not the event payload, so scaling NUM_SHARDS up as
    the company list grows needs no scheduler change.
    """
    ordered = sorted(known, key=lambda e: e.get("domain", ""))
    num_shards = max(1, -(-len(ordered) // SHARD_SIZE))  # ceil division
    shard_index = int(time.time() // SCHEDULE_INTERVAL_S) % num_shards
    shard = ordered[shard_index * SHARD_SIZE: (shard_index + 1) * SHARD_SIZE]
    return shard, shard_index, num_shards


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

    known = json.loads(known_path.read_text(encoding="utf-8"))
    shard, shard_index, num_shards = _pick_shard(known)
    shard_path = TMP / "known-shard.json"
    shard_path.write_text(json.dumps(shard, ensure_ascii=False), encoding="utf-8")

    _write_status(s3, "scraping", f"re-checking shard {shard_index + 1}/{num_shards} "
                                   f"({len(shard)} of {len(known)} known companies)")

    # 200s ceiling carried over from the pre-sharding design (see git
    # history) -- comfortably more than a ~50-company shard needs, but
    # harmless to leave generous here since the real cost driver is
    # memory x duration, not the ceiling itself.
    probe = subprocess.run(
        [sys.executable, str(ROOT / "probe.py"), "--known", str(shard_path), "--json"],
        capture_output=True, text=True, timeout=200,
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
    print(f"shard {shard_index + 1}/{num_shards}: {len(hits)}/{len(data)} re-verified, {n_jobs} jobs")
    if errors:
        print(f"{len(errors)} known boards failed to re-poll: {errors}")

    # --skip-vacuum: see this module's own docstring and
    # load_to_sqlite.py's --skip-vacuum docstring for why VACUUM moved
    # to scrape_maintenance_handler.py's own daily run instead of
    # happening on every ~5-minute shard cycle.
    _write_status(s3, "loading", f"writing {n_jobs} jobs to jobs.db")
    load = subprocess.run(
        [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
         "--resolved", str(resolved_path), "--out", str(TMP / "jobs.db"),
         "--bucket", BUCKET, "--key", "jobs.db", "--skip-vacuum"],
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
    # guard the fast-poll's own success on this step succeeding. Runs
    # against the FULL db every shard cycle, not just this shard's
    # companies -- it's a cheap DynamoDB scan + watermark check, not a
    # network-bound re-poll, so it isn't part of the cost problem
    # sharding exists to solve, and alert timeliness matters more than
    # the small saving from also sharding it.
    _write_status(s3, "sending alerts", "matching new listings against saved filters")
    alerts_result = evaluate_alerts(TMP / "jobs.db")
    if alerts_result.get("errors"):
        print(f"alert evaluation errors: {alerts_result['errors']}")

    _write_status(s3, "idle", f"last run: shard {shard_index + 1}/{num_shards}, "
                              f"{len(hits)}/{len(hits) + len(errors)} companies, {n_jobs} jobs")
    return {"shard": shard_index, "num_shards": num_shards, "hits": len(hits),
            "errors": len(errors), "jobs": n_jobs, "alerts": alerts_result}
