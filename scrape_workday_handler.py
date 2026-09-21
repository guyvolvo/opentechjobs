"""EventBridge-triggered Lambda for Workday's own fast re-poll cycle.

A dedicated function, not folded into scrape_handler.py's fast-poll
alongside everything else -- Workday was excluded from that Lambda after
a real production outage (see probe.py's WORKDAY_MAX_JOBS comment):
Workday's own connection pool competing with the rest of the fleet's, in
one shared process, for a Lambda concurrency budget too small to reserve
against. Measured live (2026-09-07) that yesterday's Israel-facet
filtering brought a full 12-company run down to ~7s from real Lambda
infrastructure, comfortably safe today -- but a SEPARATE function keeps
it that way structurally as domains.txt keeps growing, instead of
relying on re-measuring before it quietly drifts back toward the
failure point. jobs.db/known.json are a third independent writer now,
on top of the fast-poll and the once-daily discover run -- safe, same
as any number of writers, because of load_to_sqlite.py's own
conditional-write retry loop (see s3_push_conditional's docstring).

Reads its company list from companies.yml (bundled in this Lambda's own
deployment package -- see deploy-scrape-lambda.yml -- same as probe.py's
own PINS), not S3's known.json, which doesn't carry israel_facets.

Partition & Merge (2026-09-08): writes its own jobs-partition-workday.db
instead of the shared jobs.db -- scrape_handler.py's own docstring has
the full "why" (jobs.db's total size, not company count, was what
actually caused the 2026-09-08 outage). Never touches known.json at all
(never did -- companies.yml/PINS drives this Lambda instead), so
--skip-known here is a no-op in effect, just consistent with the other
writer. scrape_maintenance_handler.py's hourly merge treats this
partition as pinned, not shard-numbered: every row here is trusted as-is,
no reassignment concept applies to a hand-maintained company list -- see
loader/merge_partitions.py's own docstring.

Tenants and change detection (2026-09-21): the tenant list is
companies.yml's pins plus workday-tenants.json, which discovery fills
from Common Crawl (1,609 tenants in one snapshot against 26 pins). Each
tenant is polled on its own schedule, kept in workday-poll-state.json.gz
with loader/scrape_state.py, the fast poll's own module: one request for
page 1, whose total and newest twenty ids are the board's fingerprint,
and only a board whose fingerprint moved is walked, with its pages
fetched together. Measured live on 3M's 677: 1.4s unchanged, 12.7s
walked with ids known, 75s the first time with every description.
every_hours no longer applies to Workday tenants; the state's own
backoff does (5 minutes to 4 hours, reset on change), under this
Lambda's hourly schedule.

Known-state-gated descriptions (2026-09-08, same day, once partitioning
made this cadence affordable to shrink): Workday's own search endpoint
sends Cache-Control: no-store, no-cache and no ETag at all (confirmed
live) -- unlike Greenhouse/Lever/Ashby/SmartRecruiters, there's no
protocol-level "has this changed" to condition on. _known_external_ids_
by_domain() reads this same partition's own last-known open job ids
before probing, and probe.f_workday's own known_external_ids parameter
uses that to skip the (real cost driver) per-job description fetch for
jobs we already have -- see probe.py's own docstring for why the
description gets skipped, not the whole job. A job whose description
isn't refetched keeps whatever's already in jobs-read.db: load_to_
sqlite.py's own upsert_job() already only overwrites description when
the new value is non-empty, so an unfetched (None) description is a
correct no-op there, not a silent wipe.
"""

import gc
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import boto3

import probe

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "loader"))
import scrape_state  # noqa: E402
from deltas import put_fragment  # noqa: E402
from load_to_sqlite import s3_pull  # noqa: E402

# Reported live: "most Workday listings have no description." Workday's
# own list endpoint never has one at all -- _workday_job_detail() only
# fills it in on a per-job detail fetch, previously gated on this same
# flag being globally on (only scrape-discover.yml's once-daily full
# batch sets it). This Lambda is now the exception: the master switch
# stays on, but known_external_ids (see this module's own docstring)
# gates it per job now, not per run -- a job we already have skips the
# description fetch regardless of this flag, so the real cost is roughly
# "new/changed jobs only" rather than "every open job, every cycle."
probe.FETCH_FULL_DESCRIPTIONS = True

# Microsoft, Google, Apple and Amazon, read globally. Same Lambda for the
# same reason Workday is here: hundreds of pages a company is too slow for
# the five-minute sweep. About 33,000 roles between them, so each run
# describes only jobs this partition has no description for yet, at most
# NEW_DESCRIPTIONS_PER_RUN a company, and the first run's backlog drains
# over the next few hours instead of one run that cannot finish.
#
# Check Point rides along here for a different reason: its site is one
# 4MB page read in about a second, but it has no dates and no API, so it
# is read hourly rather than every five minutes to go easy on it.
# WP Job Openings sites (NSO) are here because nsogroup.com refuses
# GitHub's runners, and that is the only way into the sweep.
# Oracle Recruiting Cloud boards read a couple of hundred roles a page
# and want a per-job call for each full posting, so they read hourly
# with a budget rather than every five minutes.
BIG_TECH_ATS = ("microsoft", "google", "apple", "amazon", "checkpoint", "wpjobs", "oracle",
                "eightfold", "redmatch", "wprest", "tycowp")
# Two budgets, because the two kinds of description cost different things.
# Microsoft and Apple need a request per description, so theirs is a time
# budget: 300 calls fits a run beside Workday, four global reads and the
# load in 900s. Google and Amazon carry the text in their listing pages, so
# theirs is a memory budget: the first global run built all 25,000 of them
# and died at 1024MB before writing anything.
NEW_DESCRIPTIONS_PER_RUN = 1000
DETAIL_CALLS_PER_RUN = 300
CHECKPOINT_DESCRIPTIONS_PER_RUN = 100
# Left for the load and the fragments once polling is done.
BIG_TECH_TIME_RESERVE_MS = 300_000
# Discovery's Workday tenants (merge_discovered_batch.py writes it, the
# deploy bundles it), read beside companies.yml's hand-verified pins.
TENANTS_PATH = ROOT / "workday-tenants.json"
# This Lambda's own poll state, the same shape and module as the fast
# poll's (loader/scrape_state.py) under its own key: per tenant, when it
# is next due and the fingerprint of its board when last read.
WORKDAY_STATE_KEY = "workday-poll-state.json.gz"
# Tenants read at once. Each is its own host, so Workday's limits do not
# add up across them; the ceiling is this process's own connections
# (probe.session() keeps 16 per host, and a tenant uses up to 8 for
# pages plus 4 for details).
TENANT_WORKERS = 6
# Stop taking on tenants when this much time is left: the big-tech reads
# and the partition load still have to run, and the tenants already in
# flight finish first. Whatever was not reached stays due and goes first
# next run (scrape_state.due orders by overdue). Measured 2026-09-21:
# the first run stopped taking tenants at 450s left and still timed out
# at 900s, because twelve first-time tenants were in flight and PwC's
# 4,049 descriptions alone were minutes.
WORKDAY_TIME_RESERVE_MS = BIG_TECH_TIME_RESERVE_MS + 240_000
# New descriptions per tenant per run. A tenant's first read describes
# this many and leaves the rest for later runs; the gate is the set of
# jobs already described (from the partition), not merely seen, so an
# undescribed job is described on its next turn, not never.
WORKDAY_DESCRIBE_PER_TENANT = 300

TMP = Path("/tmp")
BUCKET = os.environ["DATA_BUCKET"]


def _write_status(s3, phase: str, detail: str = "") -> None:
    """Same shape and same file as scrape_handler.py's own -- see that
    function's docstring. Both Lambdas share one status.json; whichever
    ran most recently is simply what /api/pipeline-status reports.
    """
    try:
        s3.put_object(
            Bucket=BUCKET, Key="status.json", ContentType="application/json",
            Body=json.dumps({
                "phase": phase, "detail": detail, "run": "workday-poll",
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }).encode("utf-8"),
        )
    except Exception as e:
        print(f"status.json write failed (non-fatal): {e!r}")


def _known_state_by_domain() -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, str]]:
    """This partition's own currently-open jobs, per company, from
    whatever load_to_sqlite.py's own pull-modify-push cycle last wrote --
    see probe.f_workday's own known_external_ids docstring for what this
    feeds. A read-only peek, entirely separate from that cycle's own pull
    a few lines down in lambda_handler: reusing it directly would mean
    holding one sqlite3 connection open across the whole probe phase for
    no real benefit, versus just pulling this same small (pinned, ~a
    dozen companies) file twice.

    Empty dict on any failure (partition doesn't exist yet, corrupt,
    whatever) -- same fail-open posture as a missing known.json
    elsewhere: nothing here already trusted becomes wrong, everything
    just gets treated as new this one cycle (full description fetches,
    like today), which is exactly what SHOULD happen the very first time
    this ever runs, before any partition exists at all.

    Returns three maps: every open job per company, the open jobs that
    already have a description (a job is new to the big-tech reads until
    it has been described once), and when each company was last read
    successfully, taken from its jobs' last_seen, which only a successful
    poll moves. That last one is what every_hours is measured against.
    """
    path = TMP / "known-workday-state.db"
    try:
        existed, _ = s3_pull(BUCKET, "jobs-partition-workday.db", path)
        if not existed:
            return {}, {}, {}
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT company_domain, external_id, description_sha IS NOT NULL FROM jobs WHERE closed_at IS NULL"
        ).fetchall()
        polled = dict(conn.execute("SELECT company_domain, MAX(last_seen) FROM jobs GROUP BY company_domain").fetchall())
        conn.close()
    except Exception as e:
        print(f"couldn't read known Workday state (non-fatal, treating everything as new this cycle): {e!r}")
        return {}, {}, {}
    by_domain: dict[str, set[str]] = {}
    described: dict[str, set[str]] = {}
    for domain, external_id, has_description in rows:
        by_domain.setdefault(domain, set()).add(external_id)
        if has_description:
            described.setdefault(domain, set()).add(external_id)
    return by_domain, described, polled


# Slack under the interval, so a board set to every hour is still due on an
# hourly schedule that fires a minute or two early.
DUE_SLACK_S = 600


def _due(pin: dict, last_polled: str | None, now: datetime | None = None) -> bool:
    """Whether a pin is due this run. every_hours in companies.yml, default
    1. The heavy global boards that change slowly (Apple, Google, Microsoft,
    the non-AWS half of Amazon) are read every four hours instead of every
    hour, which is most of this Lambda's run time and none of what makes the
    board fresh. Unknown or unparseable history counts as due.
    """
    try:
        hours = float(pin.get("every_hours") or 1)
    except (TypeError, ValueError):
        hours = 1.0
    if not last_polled:
        return True
    try:
        last = datetime.fromisoformat(str(last_polled).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - last).total_seconds() >= hours * 3600 - DUE_SLACK_S


def _poll_big_tech(sess, ats: str, domain: str, pin: dict, described: set[str],
                   open_ids: set[str] | None = None) -> dict:
    token = pin.get("token")
    kwargs = {"known_ids": described}
    if ats == "checkpoint":
        # Its site shows no posting date, so f_checkpoint dates a role by
        # the run that first sees it, and needs every open id for that,
        # not only the described ones.
        kwargs["open_ids"] = open_ids or set()
        # A company web server behind a firewall that already turns away
        # one user agent, so its 406-role backlog fills over a few runs
        # instead of all at once.
        kwargs["description_budget"] = CHECKPOINT_DESCRIPTIONS_PER_RUN
    elif ats in ("microsoft", "apple"):
        kwargs["detail_budget"] = DETAIL_CALLS_PER_RUN
    else:
        kwargs["description_budget"] = NEW_DESCRIPTIONS_PER_RUN
    try:
        jobs = probe.FETCHERS[ats](sess, token, **kwargs)
        err = None
    except Exception as e:
        jobs, err = None, repr(e)
    if jobs is None:
        # A partial global read returns None (see probe._fetch_all), so the
        # company keeps its listings rather than losing a page's worth.
        return {"domain": domain, "ats": None, "token": None, "job_count": 0, "tried": 1,
                "error": err or f"no complete {ats} read this run", "retryable": True, "jobs": []}
    undescribed = {j.external_id for j in jobs if not j.description}
    jobs = probe._fill_classifications(jobs, domain)
    new = 0
    for j in jobs:
        if j.external_id in undescribed:
            # Skills from the title alone would replace the tags the full
            # text gave this job last time. Empty keeps the stored ones.
            j.skills = []
            continue
        new += 1
        if new > NEW_DESCRIPTIONS_PER_RUN:
            # Over this run's budget. Tags stay (they came from the full
            # text); the text itself waits, so the next run sends it.
            j.description = None
    rows = [asdict(j) for j in jobs]
    del jobs
    return {"domain": domain, "ats": ats, "token": token, "job_count": len(rows), "tried": 1,
            "error": None, "retryable": False, "jobs": rows}


def _workday_entries() -> list[dict]:
    """companies.yml's pins first, then workday-tenants.json, one entry
    per tenant and site; a pin wins over a discovered copy of itself."""
    entries = []
    seen = set()
    for domain, pin in probe.PINS.get("workday", {}).items():
        seen.add((pin["tenant"].lower(), pin["site"].lower()))
        entries.append({"domain": domain, **pin})
    if TENANTS_PATH.exists():
        domains = {e["domain"] for e in entries}
        for t in json.loads(TENANTS_PATH.read_text(encoding="utf-8")):
            key = (t["tenant"].lower(), t["site"].lower())
            if key in seen or t.get("domain") in domains:
                continue
            seen.add(key)
            domains.add(t["domain"])
            entries.append(t)
    return entries


def _poll_workday(sess, entry: dict, state_row: dict | None, known_ids: set[str] | None) -> dict:
    """One tenant: page 1 always, the rest only when the board moved.

    The fingerprint (probe.workday_fingerprint: the total and the twenty
    newest ids) stands in for the ETag Workday never sends. Equal to the
    one saved last time, with our own open set on hand to keep, the
    board is reported unchanged: one request, and load_to_sqlite leaves
    its rows alone. Different, or nothing known yet, and every page is
    fetched; the first one is handed over so it is not fetched twice.
    """
    tenant, wd, site = entry["tenant"], entry["wd"], entry["site"]
    token = f"{tenant}:{wd}:{site}"
    miss = {"domain": entry["domain"], "ats": None, "token": None, "job_count": 0,
            "tried": 1, "error": "no valid board on re-poll", "retryable": True, "jobs": []}
    try:
        page1 = probe.workday_page1(sess, tenant, wd, site)
        if page1 is None:
            return miss
        fingerprint = probe.workday_fingerprint(page1)
        if state_row and state_row.get("content_hash") == fingerprint and known_ids is not None:
            return {"domain": entry["domain"], "ats": "workday", "token": token, "job_count": len(known_ids),
                    "tried": 1, "error": None, "retryable": False, "unchanged": True,
                    "content_hash": fingerprint, "jobs": []}
        jobs = probe.f_workday(sess, tenant, wd, site, israel_facets=entry.get("israel_facets"),
                               known_external_ids=known_ids, page1=page1, describe_budget=WORKDAY_DESCRIBE_PER_TENANT)
    except Exception as e:
        return {**miss, "error": repr(e)}
    if jobs is None:
        return miss
    return {"domain": entry["domain"], "ats": "workday", "token": token, "job_count": len(jobs),
            "tried": 1, "error": None, "retryable": False, "content_hash": fingerprint,
            "jobs": [asdict(j) for j in probe._fill_classifications(jobs)]}


def lambda_handler(event, context):
    s3 = boto3.client("s3")
    entries = _workday_entries()
    big_tech = [(ats, domain, pin) for ats in BIG_TECH_ATS for domain, pin in probe.PINS.get(ats, {}).items()]
    if not entries and not big_tech:
        print("no workday tenants or big-tech pins, skipping")
        return {"skipped": True}

    known_ids, described, polled = _known_state_by_domain()
    not_due = [d for _, d, pin in big_tech if not _due(pin, polled.get(d))]
    if not_due:
        print(f"not due this run (every_hours): {', '.join(sorted(not_due))}")
    print(f"known state: {sum(len(v) for v in known_ids.values())} open jobs across "
          f"{len(known_ids)} companies from the last partition")

    state, state_etag = scrape_state.load(BUCKET, s3, key=WORKDAY_STATE_KEY)
    due = scrape_state.due(state, entries)
    print(f"{len(due)} of {len(entries)} Workday tenants due")
    _write_status(s3, "scraping", f"re-checking {len(due)} Workday tenants and {len(big_tech)} big-tech companies")

    sess = probe.session()
    results = []
    reached = 0
    with ThreadPoolExecutor(max_workers=TENANT_WORKERS) as pool:
        pending = set()
        queue = iter(due)
        while True:
            # A window of a few in flight, so stopping at the deadline
            # leaves only what is already running, not a backlog.
            while len(pending) < TENANT_WORKERS:
                if context is not None and context.get_remaining_time_in_millis() < WORKDAY_TIME_RESERVE_MS:
                    break
                entry = next(queue, None)
                if entry is None:
                    break
                pending.add(pool.submit(_poll_workday, sess, entry, state.get(entry["domain"]), described.get(entry["domain"], set()) if entry["domain"] in known_ids else None))
                reached += 1
            if not pending:
                break
            done = next(as_completed(pending))
            pending.discard(done)
            results.append(done.result())
    changed = sum(1 for r in results if r["ats"] and not r.get("unchanged"))
    unchanged = sum(1 for r in results if r.get("unchanged"))
    failed = sum(1 for r in results if not r["ats"])
    print(f"workday: {reached}/{len(due)} due tenants reached, {changed} changed, {unchanged} unchanged, {failed} failed"
          + (f", {len(due) - reached} left for next run" if reached < len(due) else ""))
    counts = scrape_state.record(state, results)
    saved = scrape_state.save(BUCKET, s3, state, state_etag, key=WORKDAY_STATE_KEY)
    print(f"poll state {'saved' if saved else 'NOT saved'}: {counts}")

    for ats, domain, pin in big_tech:
        if domain in not_due:
            continue
        if context is not None and context.get_remaining_time_in_millis() < BIG_TECH_TIME_RESERVE_MS:
            results.append({"domain": domain, "ats": None, "token": None, "job_count": 0, "tried": 0,
                            "error": "out of time this run", "retryable": True, "jobs": []})
            continue
        r = _poll_big_tech(sess, ats, domain, pin, described.get(domain, set()), known_ids.get(domain))
        print(f"{domain}: {ats} {'%d jobs' % r['job_count'] if r['ats'] else r['error']}")
        results.append(r)

    hits = [r for r in results if r["ats"]]
    n_jobs = sum(r["job_count"] for r in hits)
    print(f"{len(hits)}/{len(results)} workday and big-tech companies re-verified, {n_jobs} jobs")

    # Streamed to disk rather than built as one string beside the list it
    # came from, and freed before the loader starts: the subprocess shares
    # this Lambda's memory ceiling with everything this process still holds.
    resolved_path = TMP / "resolved-workday.json"
    with resolved_path.open("w", encoding="utf-8") as fh:
        json.dump(results, fh)

    _write_status(s3, "loading", f"writing {n_jobs} Workday jobs to jobs-partition-workday.db")
    # Was 60, matching scrape_handler.py's own (also-since-fixed) loader
    # timeout. Confirmed live (2026-09-08) this exact call hit
    # TimeoutExpired outright once jobs.db passed 1GB -- kept at 300 for
    # real margin even though this now writes a small pinned partition,
    # not the full db (see this module's own docstring). --skip-vacuum
    # for the same reason scrape_handler.py's own shard cycle passes it:
    # scrape_maintenance_handler.py owns VACUUM as part of its hourly
    # merge instead. --skip-known: see load_to_sqlite.py's own docstring
    # for that flag.
    load = subprocess.run(
        [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
         "--resolved", str(resolved_path), "--out", str(TMP / "jobs-partition-workday.db"),
         "--bucket", BUCKET, "--key", "jobs-partition-workday.db",
         "--skip-vacuum", "--skip-known", "--drop-description"],
        capture_output=True, text=True, timeout=420,
    )
    if load.stderr:
        print(load.stderr)
    if load.returncode != 0:
        _write_status(s3, "error", f"load_to_sqlite.py exited {load.returncode}")
        raise RuntimeError(f"load_to_sqlite.py exited {load.returncode}")

    # The partition above is this Lambda's own memory, not a delivery
    # mechanism. It stopped being one when delta fragments replaced the
    # partition merge: nothing has merged jobs-partition-*.db into
    # jobs-read.db since, so every Workday run since has written 139MB to
    # a file no reader opens.
    #
    # Found by asking why 700 Workday listings in the served snapshot all
    # carried the same last_seen from the previous day while this Lambda
    # was running every 30 minutes without an error. It was working
    # perfectly and delivering nowhere.
    #
    # The fragment is what reaches jobs-read.db, exactly as the fast
    # sweep's does. The partition stays because _known_external_ids_by_domain
    # reads it back to skip description re-fetches, which is the whole
    # reason a run is 65 seconds instead of many minutes.
    fragments = put_fragment(BUCKET, results)
    # Counted before the list is dropped: the status line below needs it,
    # and reading len() of the cleared list is what failed every run that
    # followed the memory fix, after its data had already gone out.
    n_results = len(results)
    results = None
    gc.collect()
    print(f"delta fragments: {len(fragments)} written"
          if fragments else "delta fragments: (nothing to apply, none written)")

    _write_status(s3, "idle", f"last run: {len(hits)}/{n_results} Workday and big-tech companies, {n_jobs} jobs")
    return {"hits": len(hits), "jobs": n_jobs, "fragments": len(fragments)}
