"""One batch of the overnight discovery-candidate merge (see
.github/workflows/merge-discovered-companies.yml, which runs this on a
schedule). Reads pending-discovery-candidates.json (640 real,
Common-Crawl-discovered, already-verified companies -- see
discover_companies.py -- sitting in a queue rather than merged all at
once), and per invocation:

  1. Safety check: the fast-poll Lambda's own most recent real Duration
     (CloudWatch), not a guess -- skips this batch entirely (queue
     untouched) if it's already using more of its 300s budget than
     BUDGET_FRACTION allows. Merging companies faster than the fast-poll
     can actually absorb them is exactly the kind of growth that made
     Workday need its own dedicated Lambda; this is the same lesson
     applied proactively instead of after an outage.
  2. Pops BATCH_SIZE candidates off the front of the queue, appends
     their guessed domains to domains.txt, resolves them for real via
     probe.py (not just trusting the earlier discovery-time guess), and
     merges the ones that resolved straight into known.json (see below
     -- Partition & Merge retired this script's own jobs.db write).
  3. Writes back whatever's left in the queue.

Leaves the actual git commit to the calling workflow -- this script
only touches local files.

Partition & Merge (2026-09-08): used to load this batch's resolved jobs
straight into the shared jobs.db, same as every other writer -- but under
partitioning, jobs.db is retired, and this batch doesn't cleanly fit
either partition shape scrape_handler.py/scrape_workday_handler.py use
(it isn't one fixed shard, and it isn't a stable pinned company list --
see loader/merge_partitions.py's own docstring for why a third shape here
risked a stale one-off batch permanently overriding fresher numbered-
shard data on every future merge). What this script actually needs isn't
"write these jobs somewhere," though -- it's "make sure the fast-poll
learns about these companies," and that only ever needed known.json, not
jobs.db itself: export_known() query (SELECT domain, ats, token FROM
companies WHERE ats IS NOT NULL) was always company-only, this script's
own resolved jobs never fed anything else downstream. So this batch's
newly-resolved companies now get upserted directly into known.json
instead of round-tripping through a jobs.db write just to export the
same rows back out. Their actual job listings reach the site on their
own next fast-poll shard rotation instead of immediately -- a real,
bounded freshness delay for brand-new companies specifically (up to one
full rotation, same latency every OTHER company's re-poll already has),
traded for not inventing a third partition shape under this same day's
already-large redesign.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "loader"))
from load_to_sqlite import s3_pull, s3_push  # noqa: E402
QUEUE_PATH = ROOT / "pending-discovery-candidates.json"
DOMAINS_PATH = ROOT / "domains.txt"

BATCH_SIZE = 80  # was 40; doubled 2026-09-08 alongside the fast-poll's own memory/timeout bump, now that a real run has proven headroom (68% memory, 31% time) at the pre-bump scale
BUDGET_FRACTION = 0.7  # don't merge more if the fast-poll is already using >70% of its budget
FAST_POLL_TIMEOUT_S = 400  # matches infra/variables.tf's scrape_lambda_timeout_s
LOG_GROUP = "/aws/lambda/iljobs-scrape-fast"


def latest_fast_poll_duration_ms() -> float | None:
    """Most recent real Duration the fast-poll Lambda reported, parsed
    straight from its own REPORT log line -- not an estimate.

    Reported live (2026-09-08): this alone wasn't the safety check it
    looked like. When probe.py's own subprocess.run(timeout=...) fires,
    the Lambda's own Duration comes back short (~90s -- exactly the
    subprocess timeout, then it raises) even though the real underlying
    work needs longer -- a hard-capped-then-failed run reads as
    "comfortably under budget" to a pure duration check, not as the
    genuine overload it is. Every single fast-poll cycle failed this way
    for 5.5 hours while this check kept reporting the duration as fine
    and let three more batches merge on top of an already-broken cycle.
    See recent_fast_poll_had_errors below, now checked alongside this.
    """
    logs = boto3.client("logs")
    try:
        resp = logs.filter_log_events(
            logGroupName=LOG_GROUP,
            filterPattern="REPORT RequestId",
            limit=5,
            interleaved=True,
        )
    except Exception as e:
        print(f"couldn't read {LOG_GROUP} ({e!r}) -- skipping this batch to be safe", file=sys.stderr)
        return None
    events = sorted(resp.get("events", []), key=lambda e: e["timestamp"], reverse=True)
    if not events:
        return None
    for e in events:
        msg = e["message"]
        marker = "Duration: "
        if marker in msg:
            try:
                return float(msg.split(marker, 1)[1].split(" ms", 1)[0])
            except (ValueError, IndexError):
                continue
    return None


def recent_fast_poll_had_errors(lookback_minutes: int = 40) -> bool | None:
    """Whether the fast-poll Lambda logged an [ERROR] (a timed-out
    subprocess, an unhandled exception, anything) in roughly the last
    few cycles. lookback_minutes covers ~8 cycles at the normal 5-min
    cadence -- long enough that one transient blip doesn't block a whole
    night's merge, short enough to catch a cycle that's now consistently
    failing, the actual 2026-09-08 incident this exists to catch.
    Returns None (treated as "unsafe, skip") if CloudWatch can't be read
    at all, same fail-safe posture as the duration check.
    """
    import time

    logs = boto3.client("logs")
    try:
        resp = logs.filter_log_events(
            logGroupName=LOG_GROUP,
            filterPattern="ERROR",
            startTime=int((time.time() - lookback_minutes * 60) * 1000),
        )
    except Exception as e:
        print(f"couldn't read {LOG_GROUP} for errors ({e!r}) -- skipping this batch to be safe", file=sys.stderr)
        return None
    return len(resp.get("events", [])) > 0


def main() -> int:
    if not QUEUE_PATH.exists():
        print("no pending-discovery-candidates.json -- nothing to do", file=sys.stderr)
        return 0

    queue = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    if not queue:
        print("queue is empty -- overnight merge is done", file=sys.stderr)
        return 0

    # Errors checked FIRST and independently of duration -- see
    # recent_fast_poll_had_errors' own docstring for why a duration
    # check alone missed 5.5 hours of the fast-poll failing outright on
    # 2026-09-08: a subprocess-timeout-then-raise reads as a short,
    # comfortably-under-budget Duration, not the overload it actually is.
    had_errors = recent_fast_poll_had_errors()
    if had_errors is None or had_errors:
        reason = "couldn't check" if had_errors is None else "logged an error recently"
        print(f"fast-poll {reason} -- skipping this batch, queue left untouched ({len(queue)} remaining)",
              file=sys.stderr)
        return 0

    duration_ms = latest_fast_poll_duration_ms()
    budget_ms = FAST_POLL_TIMEOUT_S * 1000 * BUDGET_FRACTION
    if duration_ms is None:
        print("couldn't determine the fast-poll's latest duration -- skipping this batch to be safe", file=sys.stderr)
        return 0
    print(f"fast-poll's latest real Duration: {duration_ms:.0f}ms (budget threshold: {budget_ms:.0f}ms)", file=sys.stderr)
    if duration_ms > budget_ms:
        print(f"fast-poll is already using more than {BUDGET_FRACTION:.0%} of its budget -- "
              f"skipping this batch, queue left untouched ({len(queue)} remaining)", file=sys.stderr)
        return 0

    batch = queue[:BATCH_SIZE]
    remaining = queue[BATCH_SIZE:]
    domains = [c["domain"] for c in batch]
    print(f"merging {len(batch)} candidates ({len(remaining)} will remain queued): {', '.join(domains)}", file=sys.stderr)

    existing = set(
        line.strip() for line in DOMAINS_PATH.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    new_domains = [d for d in domains if d not in existing]
    if new_domains:
        with open(DOMAINS_PATH, "a", encoding="utf-8") as f:
            f.write("\n# Common Crawl discovery batch (merge_discovered_batch.py)\n")
            for d in new_domains:
                f.write(f"{d}\n")
        print(f"appended {len(new_domains)} new lines to domains.txt ({len(domains) - len(new_domains)} already present)",
              file=sys.stderr)

    resolved_path = ROOT / "resolved-discovery-batch.json"
    # 400, not the original 180: confirmed live (2026-09-08) that
    # doubling BATCH_SIZE 40->80 without touching this let two
    # overlapping runs both hit TimeoutExpired at 180s resolving 80
    # domains with --fetch-descriptions -- the exact same "grew one
    # side without the other" pattern as every other timeout wall
    # tonight. Matches infra/variables.tf's own scrape_fast pattern of
    # generous headroom rather than a number chasing today's batch size.
    # No --fetch-descriptions: this batch's own job listings are never
    # used downstream anymore (see this module's own docstring) -- only
    # domain/ats/token survive, into known.json. The regular fast-poll
    # re-resolves the real job data (without descriptions either, same
    # as any other company) on this company's own next shard rotation.
    # Paying for Comeet/Workday's extra per-job detail fetch here would
    # be pure waste now.
    probe = subprocess.run(
        [sys.executable, str(ROOT / "probe.py"), "--domain", ",".join(domains), "--json"],
        capture_output=True, text=True, timeout=400,
    )
    if probe.stderr:
        print(probe.stderr, file=sys.stderr)
    if probe.returncode != 0:
        print(f"probe.py exited {probe.returncode} -- batch NOT merged, queue left untouched", file=sys.stderr)
        return 1
    resolved_path.write_text(probe.stdout, encoding="utf-8")

    data = json.loads(probe.stdout)
    hits = [r for r in data if r.get("ats")]
    print(f"{len(hits)}/{len(data)} resolved for real", file=sys.stderr)

    bucket = os.environ.get("DATA_BUCKET")
    if bucket and hits:
        # Upsert, not export_known()'s wholesale replace: this batch only
        # ever sees its own ~80 companies, never the full set, so a
        # replace here would wipe out every other company known.json
        # already had. See this module's own docstring for why this
        # replaced a jobs.db write entirely rather than becoming a third
        # partition shape.
        known_path = ROOT / "known-for-discovery-merge.json"
        existed, _ = s3_pull(bucket, "known.json", known_path)
        known = json.loads(known_path.read_text(encoding="utf-8")) if existed else []
        by_domain = {e["domain"]: e for e in known}
        for r in hits:
            by_domain[r["domain"]] = {"domain": r["domain"], "ats": r["ats"], "token": r.get("token")}
        known_path.write_text(json.dumps(list(by_domain.values()), ensure_ascii=False), encoding="utf-8")
        s3_push(bucket, "known.json", known_path)
        print(f"merged {len(hits)} newly-resolved companies into known.json ({len(by_domain)} total)",
              file=sys.stderr)
    elif not bucket:
        print("DATA_BUCKET not set -- skipping known.json update (local/test run)", file=sys.stderr)

    QUEUE_PATH.write_text(json.dumps(remaining, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"batch complete, {len(remaining)} candidates remain queued", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
