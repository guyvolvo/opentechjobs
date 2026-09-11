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
own hourly run owns VACUUM now, as part of merging every shard's own
partition into one file (see below).

Partition & Merge (2026-09-08): this Lambda writes its own shard's
jobs-partition-{shard_index}.db instead of the shared jobs.db --
sharding alone decoupled the PROBE cost from company count, but
load_to_sqlite.py's own pull-modify-push cycle still downloaded and
uploaded the FULL jobs.db every invocation regardless of shard size,
which is what actually caused the 2026-09-08 outage (jobs.db passed
795MB, both scrape Lambdas started hitting Runtime.OutOfMemory). Writing
only this shard's own partition means that cost -- and the OOM risk --
scale with SHARD_SIZE, not total company count, for real this time.
scrape_maintenance_handler.py's own hourly run merges every partition
back into jobs-read.db, which is what api/db.py actually reads; see
loader/merge_partitions.py's own docstring for that half.

Runs probe.py --known and loader/load_to_sqlite.py as subprocesses
against /tmp, exactly the same two commands scrape-fast.yml already ran
-- reusing those already-proven CLI entry points rather than
re-implementing their logic here.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from sharding import SHARD_SIZE, current_shard_index, num_shards_for, ordered_domains

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "loader"))

from deltas import put_fragment  # noqa: E402
import scrape_state  # noqa: E402
TMP = Path("/tmp")
BUCKET = os.environ["DATA_BUCKET"]

# Must match the EventBridge schedule's own real interval in seconds --
# see scrape_lambda.tf's own environment block for why this is passed in
# rather than hardcoded: during the 2026-09-08 interim cost cut, this
# constant stayed at 300 while the actual schedule moved to 1200s,
# silently skipping some shards' rotation entirely rather than just
# slowing it down (see current_shard_index's own docstring). Defaults to
# 300 only for a bare local run outside the Lambda environment.
SCHEDULE_INTERVAL_S = int(os.environ.get("SCHEDULE_INTERVAL_S", "300"))

# The old DynamoDB validator table. Read once, only when the S3 poll
# state is missing, to seed it (see scrape_state.load). Everything else
# about conditional polling lives in that object now. Unset it and the
# first run after a wipe just refetches every board once.
STATE_TABLE = os.environ.get("SCRAPE_STATE_TABLE")





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
    """Which shard runs THIS invocation. The domain -> shard assignment
    itself now lives in sharding.py, shared with loader/merge_partitions.py
    -- see that module's own docstring for why the two must never drift
    apart. Only the wall-clock "which shard runs right now" part stays
    here, since that's specific to this Lambda's own schedule.
    """
    by_domain = {e.get("domain", ""): e for e in known}
    domains = ordered_domains(known)
    num_shards = num_shards_for(len(domains))
    shard_index = current_shard_index(num_shards, SCHEDULE_INTERVAL_S)
    shard_domains = domains[shard_index * SHARD_SIZE: (shard_index + 1) * SHARD_SIZE]
    shard = [by_domain[d] for d in shard_domains]
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

    # Every company, every run. Sharding existed because a poll used to
    # mean downloading and parsing a whole board, so the only way to keep
    # cost flat as the company list grew was to check 1/NUM_SHARDS of it
    # per invocation -- which is precisely why a new job took up to 5.8
    # hours to be noticed. Conditional polling removed that cost:
    # measured live, a 50-company shard where everything answered 304
    # completed in 2.3s, and most of even that was the partition round
    # trip rather than the polling. Sweeping all of them costs seconds.
    num_shards = num_shards_for(len(known))
    # Every board is a candidate; scrape_state.due decides which are
    # actually polled. The old fixed-window rotation is gone: it split
    # the list by position, which is unrelated to whether a board has
    # anything new, so it delayed busy boards and still polled dead ones
    # on a schedule.
    sweep = known

    # Poll state: which boards are due, and the validators to send them.
    #
    # Both used to be separate problems. The validators lived in
    # DynamoDB (~286,000 reads a day for 200KB of mostly-static text) and
    # every board was polled every five minutes regardless of whether it
    # had ever changed. Measured over 27 consecutive sweeps, 101 of 3,434
    # boards changed at all. The other 97% answered "nothing changed"
    # twenty-seven times in a row.
    #
    # Now one gzipped S3 object holds both, and each board carries its
    # own interval: reset to 3 minutes by a change, backed off by 1.5x
    # per quiet poll up to 20. See loader/scrape_state.py.
    poll_state, state_etag = scrape_state.load(BUCKET, s3, STATE_TABLE)
    sweep = scrape_state.due(poll_state, sweep)
    if not sweep:
        # Everything is inside its own interval. Nothing to do, and
        # saying so costs a second rather than a full sweep.
        _write_status(s3, "idle", "no boards due this tick")
        print("no boards due")
        return {"swept": 0, "unchanged": 0, "changed": 0, "fragments": 0,
                "num_shards": 0, "hits": 0, "errors": 0, "jobs": 0}

    shard_path = TMP / "known-shard.json"
    shard_path.write_text(json.dumps(sweep, ensure_ascii=False), encoding="utf-8")

    _write_status(s3, "scraping", f"sweeping {len(sweep)} of {len(known)} companies due now")

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
    unchanged = [r for r in data if r.get("unchanged")]
    n_jobs = sum(r["job_count"] for r in hits)
    sched = scrape_state.record(poll_state, data)
    saved = scrape_state.save(BUCKET, s3, poll_state, state_etag)
    changed = [r for r in data if r.get("ats") and not r.get("unchanged")]
    print(f"sweep: {len(hits)}/{len(data)} re-verified, {n_jobs} jobs, "
          f"{len(unchanged)} unchanged, poll state {'saved' if saved else 'NOT saved'} "
          f"({sched['changed']} reset to floor, {sched['unchanged']} backed off, "
          f"{sched['errored']} held)")
    if errors:
        print(f"{len(errors)} known boards failed to re-poll: {len(errors)} companies")

    # One small fragment, not N partition rewrites. This is the change
    # that makes a wide sweep affordable: persisting a sweep used to mean
    # a 48MB pull-modify-push per shard it touched, 50-170s each, and a
    # run wanting 18 of them finished 1 and discarded the rest. A
    # fragment holds only the companies that actually changed, so the
    # sweep never opens a database at all and the write is kilobytes.
    #
    # The 5-minute applier (scrape_maintenance_handler.py) replays these
    # into jobs-read.db. Fragments survive until it has successfully
    # pushed a snapshot containing them, so a crash here or there costs a
    # repeat, never a listing.
    _write_status(s3, "loading", f"writing delta for {len(changed)} changed companies")
    fragments = put_fragment(BUCKET, data)
    print(f"delta fragments: {len(fragments)} written"
          if fragments else "delta fragments: (nothing changed, none written)")

    _write_status(s3, "idle", f"last sweep: {len(data)} companies, {len(unchanged)} unchanged, "
                              f"{len(changed)} changed, {n_jobs} jobs")
    return {"swept": len(data), "unchanged": len(unchanged), "changed": len(changed),
            "fragments": len(fragments), "num_shards": num_shards, "hits": len(hits),
            "errors": len(errors), "jobs": n_jobs}
