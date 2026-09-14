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

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import boto3

import probe

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "loader"))
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
BIG_TECH_ATS = ("microsoft", "google", "apple", "amazon")
# 400, measured: Apple's detail calls ran 800 in about 220s from a desk,
# and a run has Workday, four global reads and the load to fit in 900s.
NEW_DESCRIPTIONS_PER_RUN = 400
# Left for the load and the fragments once polling is done.
BIG_TECH_TIME_RESERVE_MS = 300_000

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


def _known_state_by_domain() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
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

    Returns two maps: every open job per company, and the open jobs that
    already have a description. The second is what the big-tech reads use:
    a job is new to them until it has been described once.
    """
    path = TMP / "known-workday-state.db"
    try:
        existed, _ = s3_pull(BUCKET, "jobs-partition-workday.db", path)
        if not existed:
            return {}, {}
        conn = sqlite3.connect(path)
        rows = conn.execute(
            "SELECT company_domain, external_id, description_sha IS NOT NULL FROM jobs WHERE closed_at IS NULL"
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"couldn't read known Workday state (non-fatal, treating everything as new this cycle): {e!r}")
        return {}, {}
    by_domain: dict[str, set[str]] = {}
    described: dict[str, set[str]] = {}
    for domain, external_id, has_description in rows:
        by_domain.setdefault(domain, set()).add(external_id)
        if has_description:
            described.setdefault(domain, set()).add(external_id)
    return by_domain, described


def _poll_big_tech(sess, ats: str, domain: str, pin: dict, described: set[str]) -> dict:
    token = pin.get("token")
    kwargs = {"known_ids": described}
    if ats in ("microsoft", "apple"):
        kwargs["detail_budget"] = NEW_DESCRIPTIONS_PER_RUN
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
    return {"domain": domain, "ats": ats, "token": token, "job_count": len(jobs), "tried": 1,
            "error": None, "retryable": False, "jobs": [asdict(j) for j in jobs]}


def lambda_handler(event, context):
    s3 = boto3.client("s3")
    pins = probe.PINS.get("workday", {})
    big_tech = [(ats, domain, pin) for ats in BIG_TECH_ATS for domain, pin in probe.PINS.get(ats, {}).items()]
    if not pins and not big_tech:
        print("no workday or big-tech pins in companies.yml, skipping")
        return {"skipped": True}

    known_ids, described = _known_state_by_domain()
    print(f"known state: {sum(len(v) for v in known_ids.values())} open jobs across "
          f"{len(known_ids)} companies from the last partition")

    _write_status(s3, "scraping", f"re-checking {len(pins)} Workday and {len(big_tech)} big-tech companies")

    sess = probe.session()
    results = []
    for domain, pin in pins.items():
        try:
            jobs = probe.f_workday(sess, pin["tenant"], pin["wd"], pin["site"], israel_facets=pin.get("israel_facets"),
                                    known_external_ids=known_ids.get(domain))
        except Exception as e:
            jobs, err = None, repr(e)
        else:
            err = None
        if jobs is None:
            # Same shape/reasoning as refetch_known()'s own MISS handling
            # (probe.py): a single re-poll failing on an already-trusted
            # board is transient, not a confident correction -- retryable
            # so load_resolved() leaves this domain's existing ats/token
            # alone rather than demoting it.
            results.append({
                "domain": domain, "ats": None, "token": None, "job_count": 0,
                "tried": 1, "error": err or "no valid board on re-poll", "retryable": True, "jobs": [],
            })
            continue
        token = f"{pin['tenant']}:{pin['wd']}:{pin['site']}"
        results.append({
            "domain": domain, "ats": "workday", "token": token,
            "job_count": len(jobs), "tried": 1, "error": None, "retryable": False,
            "jobs": [asdict(j) for j in probe._fill_classifications(jobs)],
        })

    for ats, domain, pin in big_tech:
        if context is not None and context.get_remaining_time_in_millis() < BIG_TECH_TIME_RESERVE_MS:
            results.append({"domain": domain, "ats": None, "token": None, "job_count": 0, "tried": 0,
                            "error": "out of time this run", "retryable": True, "jobs": []})
            continue
        r = _poll_big_tech(sess, ats, domain, pin, described.get(domain, set()))
        print(f"{domain}: {ats} {'%d jobs' % r['job_count'] if r['ats'] else r['error']}")
        results.append(r)

    hits = [r for r in results if r["ats"]]
    n_jobs = sum(r["job_count"] for r in hits)
    print(f"{len(hits)}/{len(results)} workday and big-tech companies re-verified, {n_jobs} jobs")

    resolved_path = TMP / "resolved-workday.json"
    resolved_path.write_text(json.dumps(results), encoding="utf-8")

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
    print(f"delta fragments: {len(fragments)} written"
          if fragments else "delta fragments: (nothing to apply, none written)")

    _write_status(s3, "idle", f"last run: {len(hits)}/{len(results)} Workday companies, {n_jobs} jobs")
    return {"hits": len(hits), "jobs": n_jobs, "fragments": len(fragments)}
