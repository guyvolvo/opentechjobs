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
"""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import boto3

import probe

ROOT = Path(__file__).parent
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


def lambda_handler(event, context):
    s3 = boto3.client("s3")
    pins = probe.PINS.get("workday", {})
    if not pins:
        print("no workday pins in companies.yml, skipping")
        return {"skipped": True}

    _write_status(s3, "scraping", f"re-checking {len(pins)} Workday companies")

    sess = probe.session()
    results = []
    for domain, pin in pins.items():
        try:
            jobs = probe.f_workday(sess, pin["tenant"], pin["wd"], pin["site"], israel_facets=pin.get("israel_facets"))
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

    hits = [r for r in results if r["ats"]]
    n_jobs = sum(r["job_count"] for r in hits)
    print(f"{len(hits)}/{len(results)} workday companies re-verified, {n_jobs} jobs")

    resolved_path = TMP / "resolved-workday.json"
    resolved_path.write_text(json.dumps(results), encoding="utf-8")

    _write_status(s3, "loading", f"writing {n_jobs} Workday jobs to jobs.db")
    # 60, matching scrape_handler.py's own loader timeout -- see that
    # file's comment on why 25 wasn't enough headroom for a real
    # conditional-write retry.
    load = subprocess.run(
        [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
         "--resolved", str(resolved_path), "--out", str(TMP / "jobs.db"),
         "--bucket", BUCKET, "--key", "jobs.db"],
        capture_output=True, text=True, timeout=60,
    )
    if load.stderr:
        print(load.stderr)
    if load.returncode != 0:
        _write_status(s3, "error", f"load_to_sqlite.py exited {load.returncode}")
        raise RuntimeError(f"load_to_sqlite.py exited {load.returncode}")

    _write_status(s3, "idle", f"last run: {len(hits)}/{len(results)} Workday companies, {n_jobs} jobs")
    return {"hits": len(hits), "jobs": n_jobs}
