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

from alerts import evaluate_alerts
from sharding import SHARD_SIZE, build_shard_map, current_shard_index, num_shards_for, ordered_domains

ROOT = Path(__file__).parent
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

# Conditional-poll validators (infra/dynamodb.tf's scrape_state table).
# Optional on purpose: an unset table name, or any failure reading it,
# degrades to a normal full fetch rather than failing the run. Losing the
# optimization is a cost problem; failing the run is a data problem.
STATE_TABLE = os.environ.get("SCRAPE_STATE_TABLE")

# How many runs it takes to cover every company. 1 is a true global
# sweep and the destination; anything higher splits the list into that
# many rotating windows.
#
# Not 1 yet, and the reason is measured rather than cautious. A sweep is
# only cheap when companies answer 304, and that needs a stored
# validator: right now 1,642 of 3,491 have one (47%), because shards
# 40-69 spent hours unable to persist anything (see s3_pull's own
# AccessDenied note). The remaining 1,849 are full fetches, and a global
# sweep of all of them ran past probe.py's 200s subprocess timeout and
# killed the run outright, which is worse than the rotation it replaced.
#
# 4 windows is ~873 companies per run, full coverage every 20 minutes,
# still 17x better than the 5.8-hour rotation. Drop this to 1 once
# validator coverage is high enough that a full sweep fits comfortably;
# the code path is identical either way.
SWEEP_WINDOWS = max(1, int(os.environ.get("SWEEP_WINDOWS", "4")))


def _load_scrape_state(domains: list[str]) -> dict[str, dict]:
    """domain -> {etag, last_modified, content_hash} for this shard.

    One BatchGetItem per 100 keys, which at SHARD_SIZE=50 is a single
    round trip. Kept out of the partition file deliberately: reading a
    validator out of a 30-90MB S3 object would cost more than the fetch
    it saves.
    """
    if not STATE_TABLE or not domains:
        return {}
    try:
        client = boto3.client("dynamodb")
        out: dict[str, dict] = {}
        for i in range(0, len(domains), 100):
            chunk = domains[i:i + 100]
            resp = client.batch_get_item(
                RequestItems={STATE_TABLE: {
                    "Keys": [{"domain": {"S": d}} for d in chunk],
                    "ProjectionExpression": "#d, etag, last_modified, content_hash",
                    "ExpressionAttributeNames": {"#d": "domain"},
                }}
            )
            for item in resp.get("Responses", {}).get(STATE_TABLE, []):
                out[item["domain"]["S"]] = {
                    k: item[k]["S"] for k in ("etag", "last_modified", "content_hash") if k in item
                }
        return out
    except Exception as e:
        print(f"couldn't read scrape state (non-fatal, falling back to full fetches): {e!r}")
        return {}


def _save_scrape_state(results: list[dict]) -> int:
    """Persist whatever validators this run learned.

    Only rows that actually carry one are written, so an unchanged 304
    (which carries the same validator it was given) costs no write.
    """
    if not STATE_TABLE:
        return 0
    puts = []
    for r in results:
        if r.get("unchanged") or not r.get("ats"):
            continue  # nothing new learned, or nothing worth trusting
        item = {"domain": {"S": r["domain"]}}
        for key in ("etag", "last_modified", "content_hash"):
            if r.get(key):
                item[key] = {"S": str(r[key])}
        if len(item) > 1:
            puts.append({"PutRequest": {"Item": item}})
    if not puts:
        return 0
    try:
        client = boto3.client("dynamodb")
        for i in range(0, len(puts), 25):  # BatchWriteItem's own hard limit
            client.batch_write_item(RequestItems={STATE_TABLE: puts[i:i + 25]})
        return len(puts)
    except Exception as e:
        print(f"couldn't save scrape state (non-fatal, next run just refetches): {e!r}")
        return 0


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
    #
    # SWEEP_WINDOWS controls how much of that is done per run. At 1 this
    # is the global sweep; above 1 it rotates through equal windows, in
    # the same stable domain order the shard map uses, so coverage is
    # complete every SWEEP_WINDOWS runs rather than every 70.
    shard_map = build_shard_map(known)
    num_shards = num_shards_for(len(known))
    if SWEEP_WINDOWS == 1:
        sweep = known
        window_desc = "all"
    else:
        by_domain = {e.get("domain", ""): e for e in known}
        domains = ordered_domains(known)
        window = current_shard_index(SWEEP_WINDOWS, SCHEDULE_INTERVAL_S)
        size = -(-len(domains) // SWEEP_WINDOWS)  # ceil
        sweep = [by_domain[d] for d in domains[window * size:(window + 1) * size]]
        window_desc = f"window {window + 1}/{SWEEP_WINDOWS} of"

    # Whatever validators we already hold. refetch_known sends them as
    # If-None-Match, and ~88% of tracked companies sit on an ATS that
    # answers 304 to one.
    state = _load_scrape_state([e["domain"] for e in sweep])
    for e in sweep:
        e.update(state.get(e["domain"], {}))
    shard_path = TMP / "known-shard.json"
    shard_path.write_text(json.dumps(sweep, ensure_ascii=False), encoding="utf-8")

    _write_status(s3, "scraping", f"sweeping {window_desc} {len(sweep)} of {len(known)} companies")

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
    saved = _save_scrape_state(data)
    print(f"sweep: {len(hits)}/{len(data)} re-verified, {n_jobs} jobs, "
          f"{len(unchanged)} unchanged, {saved} validators stored")
    if errors:
        print(f"{len(errors)} known boards failed to re-poll: {len(errors)} companies")

    # The sweep polls every company, but writes stay per-shard. One
    # global partition would be the monolith that partitioning exists to
    # avoid -- partitions still carry description and raw_json, since
    # that text is what the merge builds jobs_fts from, so a single file
    # holding all of them would be the ~1.2GB object every writer used to
    # pull and push on every run.
    #
    # So results are grouped back into the shard each company already
    # belongs to, and only shards containing at least one CHANGED company
    # are written at all. In steady state that is a handful of small
    # files rather than 70, because conditional polling means most
    # companies come back 304 with nothing to store.
    by_shard: dict[int, list[dict]] = {}
    for r in data:
        idx = shard_map.get(r["domain"])
        if idx is not None:
            by_shard.setdefault(idx, []).append(r)

    # A shard with only unchanged companies has nothing new to record.
    # Skipping it avoids an S3 read and write per shard per sweep, which
    # is the difference between this being affordable and not. The cost
    # is that those companies' last_checked doesn't advance this run;
    # harmless, since MAX(last_checked) across the snapshot still moves
    # whenever anything at all changes.
    changed_shards = {i: rs for i, rs in by_shard.items()
                       if any(not r.get("unchanged") and r.get("ats") for r in rs)}
    print(f"{len(changed_shards)} of {len(by_shard)} shards have changes to write")

    written, touched_partitions = 0, []
    for idx, results in sorted(changed_shards.items()):
        partition_key = f"jobs-partition-{idx}.db"
        partition_path = TMP / partition_key
        part_resolved = TMP / f"resolved-{idx}.json"
        part_resolved.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
        _write_status(s3, "loading", f"writing shard {idx} ({len(results)} companies)")
        load = subprocess.run(
            [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
             "--resolved", str(part_resolved), "--out", str(partition_path),
             "--bucket", BUCKET, "--key", partition_key, "--skip-vacuum", "--skip-known"],
            capture_output=True, text=True, timeout=300,
        )
        if load.stderr:
            print(load.stderr)
        if load.returncode != 0:
            # One bad shard must not discard the rest of the sweep's
            # work, which is the whole point of writing them separately.
            print(f"load_to_sqlite.py exited {load.returncode} for shard {idx}, continuing")
            continue
        written += 1
        touched_partitions.append(partition_path)
        part_resolved.unlink(missing_ok=True)

    print(f"wrote {written}/{len(changed_shards)} changed partitions")
    if written == 0 and changed_shards:
        _write_status(s3, "error", "every changed shard failed to load")
        raise RuntimeError("every changed shard failed to load")

    _write_status(s3, "sending alerts", "matching new listings against saved filters")
    # Over each partition this sweep refreshed, not one shard's. A
    # company's alerts only need checking when its own data just moved,
    # which is exactly what changed_shards contains.
    alerts_result = {"errors": []}
    for partition_path in touched_partitions:
        result = evaluate_alerts(partition_path)
        alerts_result["errors"] += result.get("errors") or []
        for k, v in result.items():
            if k != "errors" and isinstance(v, int):
                alerts_result[k] = alerts_result.get(k, 0) + v
    if alerts_result.get("errors"):
        print(f"alert evaluation errors: {alerts_result['errors']}")

    _write_status(s3, "idle", f"last sweep: {len(data)} companies, {len(unchanged)} unchanged, "
                              f"{written} partitions written, {n_jobs} jobs")
    return {"swept": len(data), "unchanged": len(unchanged), "partitions_written": written,
            "num_shards": num_shards, "hits": len(hits), "errors": len(errors),
            "jobs": n_jobs, "alerts": alerts_result}
