"""Builds bootstrap.json: the board's default first page, precomputed at
merge time and served as a static file from CloudFront's edge.

Why this exists. api/db.py downloads the whole ~1.26GB jobs-read.db
inside a user's request whenever its ETag moves, so every merge and every
cold start bills a full transfer to whoever happens to arrive next.
Measured over 809 real requests: median 1.49s, p90 9.08s, max 25.00s,
which is the API Lambda's own timeout, meaning some of those requests
didn't just feel slow, they failed. The default unfiltered view is by far
the most requested thing in the system and none of that work is needed
for it.

Why it costs no freshness. jobs-read.db only changes when the merge runs.
Between merges the API's answer to the default query is identical, and
the frontend renders relative ages client-side from posted_at rather than
from any string baked in here. So this file is exactly as current as the
live API would be, not a stale approximation of it.

Why the query is written out here rather than importing route_jobs.
api/handler.py builds a DynamoDB table handle and api/db.py an S3 client
at import time, both against environment this Lambda has no reason to
carry, and api/db.py would also collide with the maintenance package's
own db/ directory. The cost of copying is drift, so the payload records
the exact query-string it represents and the frontend refuses to use it
unless that matches the request it was about to make itself. If these
defaults ever diverge, the site silently falls back to a normal fetch
instead of rendering something subtly wrong.
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from job_filters import build_jobs_where, has_places, register_functions  # noqa: E402

# Must match what frontend/app.js actually requests for the unfiltered
# default view: currentFilterParams() with everything empty leaves only
# confidence, then sort/dir/limit/offset. PAGE_SIZE is 50 there. qs()
# drops empty values, which is why no other keys appear.
BOOTSTRAP_PARAMS = "confidence=all&roles=tech&sort=age&dir=asc&limit=50&offset=0"
PAGE_SIZE = 50

# The views worth precomputing, as file name to the filter behind it.
# Two, and there is a reason for each. The empty one is the board's
# default. country=IL is the other because the geo prompt's own accept
# button asks for exactly this, which made it the most requested view
# with nothing behind it: measured over 50 cold visits, the first five
# took 14.3 seconds each while the other 45 took under 17ms, all of it a
# cold API instance downloading the whole snapshot before it could
# answer. Anything beyond these two is guesswork, and a precomputed page
# nobody asks for is just a file to keep correct.
#
# currentFilterParams() emits country ahead of confidence, so that is the
# order here. The frontend compares this string exactly.
# roles=tech on both: the board opens on tech roles (api/role_class.py)
# since 2026-09-21, and that is the view these files stand in for.
VIEWS = {
    "bootstrap.json": {"roles": "tech"},
    "bootstrap-il.json": {"country": "IL", "roles": "tech"},
}


def params_for(filters: dict) -> str:
    """The query string app.js builds for this view, in its order."""
    lead = "".join(f"{k}={v}&" for k, v in filters.items())
    return lead + BOOTSTRAP_PARAMS

# Same column list as route_jobs. Notably no `description`: the list
# endpoint doesn't return one either, which is why the whole page is
# 4.4KB gzipped.
_COLUMNS = """
    id, company_domain, ats, title, location, department,
    category_of(department, title) AS category, seniority, workplace_type, url,
    posted_at, confidence, first_seen, last_seen, closed_at,
    skills, salary_text, salary_is_estimate
"""

# Same fallback api/handler.py uses, and for the same reason: this runs
# against whatever snapshot the merge just built, which may predate the
# column. It has to match the API's answer exactly, because the two
# render the same view moments apart and any disagreement shows up as a
# flicker on the first page every reader sees.
_SALARY_SOURCE_FALLBACK = (
    "CASE WHEN salary_text IS NULL OR salary_text = '' THEN NULL "
    "WHEN salary_is_estimate = 1 THEN 'table' ELSE 'disclosed' END AS salary_source"
)


def _salary_source_select(conn) -> str:
    try:
        if any(r[1] == "salary_source" for r in conn.execute("PRAGMA table_info(jobs)")):
            return "salary_source"
    except Exception:
        pass
    return _SALARY_SOURCE_FALLBACK

def build(db_path: Path, filters: dict | None = None) -> dict:
    """Read the just-merged snapshot and return the payload to publish.

    The WHERE clause comes from build_jobs_where rather than being
    written out here, which is the one part of this file that must not
    drift: the country filter reads a column that only some snapshots
    carry (has_places), and getting that wrong means publishing a page
    that quietly disagrees with the API it is standing in for.
    """
    filters = filters or {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    register_functions(conn)  # category_of(), same as api/db.py does
    try:
        places = has_places(conn)
        # build_jobs_where drops a country filter it cannot answer, which
        # is right for the API (a reader sees too much for one merge
        # cycle rather than an error) and wrong here. Publishing a global
        # first page under the params string country=IL would hand every
        # Israeli visitor the whole board and look like the real thing.
        # Better to publish nothing and let the API answer.
        if filters.get("country") and not places:
            raise ValueError("this snapshot has no country column yet")
        where, args = build_jobs_where({"confidence": "all", **filters}, places=places)
        total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", args).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT {_COLUMNS}, {_salary_source_select(conn)}
            FROM jobs
            WHERE {where}
            -- datetime(), and NULLs last, matching route_jobs exactly:
            -- posted_at is TEXT and rows predating _normalize_date can
            -- carry other offsets, which a lexicographic sort gets wrong.
            -- id last, as route_jobs does, so tied posting times come out
            -- in the same order here as on the API's own first page.
            ORDER BY posted_at IS NULL, datetime(posted_at) DESC, id DESC
            LIMIT ? OFFSET 0
            """,
            (*args, PAGE_SIZE),
        ).fetchall()
    finally:
        conn.close()

    return {
        # The frontend compares this against the query string it was
        # about to send and ignores the whole file on any mismatch.
        "params": params_for(filters),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "jobs": {
            "jobs": [dict(r) for r in rows],
            "total": total,
            "limit": PAGE_SIZE,
            "offset": 0,
        },
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, type=Path, help="local jobs-read.db to read")
    ap.add_argument("--out", required=True, type=Path, help="where to write the payload")
    ap.add_argument("--country", help="build the country view instead of the default")
    args = ap.parse_args()
    payload = build(args.db, {"country": args.country} if args.country else {})
    args.out.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes, "
          f"{len(payload['jobs']['jobs'])} of {payload['jobs']['total']} jobs)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
