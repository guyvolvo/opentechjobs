"""
OpenMarketIL API. One Lambda behind CloudFront (/api/* routes here, see
infra/cloudfront.tf).

No framework: a handful of routes over a small SQLite file plus a
DynamoDB table, and an if/elif router is as clear as a micro-framework
without the extra weight.

Job data itself stays read-only, from the batch loader
(loader/load_to_sqlite.py) alone -- starring a listing is still
client-local in localStorage, not accounts-backed. The one real write
surface is /me/alerts: Cognito-authenticated (see infra/apigateway.tf's
JWT authorizer, attached only to those routes), DynamoDB-backed, scoped
to the caller's own sub claim. Every other route stays fully public, no
auth required, matching this project's original "no accounts" framing
minus the one feature that genuinely needed one -- see PRODUCT.md.
"""

import json
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import boto3
from boto3.dynamodb.conditions import Key

from db import get_connection
from job_filters import FRESH_CLAUSE, IL_KEYWORDS, bool_param, build_jobs_where

_alerts_table = boto3.resource("dynamodb").Table(os.environ["ALERTS_TABLE"])

# What build_jobs_where() actually reads -- rejecting anything else at
# creation time catches a typo'd filter key immediately instead of it
# silently matching nothing forever, since the evaluator (alerts.py)
# just feeds this same dict straight into that same function.
_ALLOWED_FILTER_KEYS = {
    "q", "keywords", "ats", "company", "department", "seniority", "location",
    "workplace", "confidence", "israel_only", "include_closed", "include_outdated",
    "min_age_days", "max_age_days",
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
}

SORT_COLUMNS = {
    "age": "posted_at",
    "company": "company_domain",
    "title": "title",
    "location": "location",
    "ats": "ats",
}

# IL_KEYWORDS/FRESH_CLAUSE/BOARD_MAX_AGE_DAYS now live in job_filters.py --
# shared with the alert evaluator, which needs the exact same matching
# logic, not a second copy that quietly drifts from this one.


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    if method == "OPTIONS":
        return _response(204, "")

    path = event.get("rawPath") or "/"
    if path.startswith("/api"):
        path = path[4:] or "/"
    params = _query_params(event)

    try:
        if path == "/jobs":
            return _response(200, json.dumps(route_jobs(params), default=str))
        if path.startswith("/jobs/") and len(path) > len("/jobs/"):
            job = route_job_detail(path[len("/jobs/"):])
            if job is None:
                return _response(404, json.dumps({"error": "no job with that id"}))
            return _response(200, json.dumps(job, default=str))
        if path == "/companies":
            return _response(200, json.dumps(route_companies(params), default=str))
        if path == "/stats":
            return _response(200, json.dumps(route_stats(params), default=str))
        if path == "/health":
            return _response(200, json.dumps(route_health(), default=str))
        if path == "/me/alerts":
            claims = _authenticated_claims(event)
            if method == "GET":
                return _response(200, json.dumps(route_list_alerts(claims["sub"]), default=str))
            if method == "POST":
                body = json.loads(event.get("body") or "{}")
                return _response(201, json.dumps(route_create_alert(claims, body), default=str))
            return _response(405, json.dumps({"error": "method not allowed"}))
        if path.startswith("/me/alerts/") and len(path) > len("/me/alerts/"):
            user_id = _authenticated_claims(event)["sub"]
            alert_id = path[len("/me/alerts/"):]
            if method == "PATCH":
                body = json.loads(event.get("body") or "{}")
                updated = route_update_alert(user_id, alert_id, body)
                if updated is None:
                    return _response(404, json.dumps({"error": "no alert with that id"}))
                return _response(200, json.dumps(updated, default=str))
            if method == "DELETE":
                route_delete_alert(user_id, alert_id)
                return _response(204, "")
            return _response(405, json.dumps({"error": "method not allowed"}))
        return _response(404, json.dumps({"error": f"no route for {path}"}))
    except ValueError as e:
        return _response(400, json.dumps({"error": str(e)}))
    except Exception as e:  # last resort: never leak a raw traceback to callers
        return _response(500, json.dumps({"error": "internal error", "detail": str(e)}))


def _authenticated_claims(event) -> dict:
    """API Gateway's JWT authorizer (infra/apigateway.tf) already
    validated the token's signature and expiry before this Lambda ever
    ran -- these routes are only reachable at all with a genuine Cognito
    JWT. This just reads the claims it already checked. sub (not
    username) is the stable per-user id used everywhere below: identical
    whether the caller signed in with Google, GitHub, or email, unlike
    username (which for GitHub is "github_<id>", for email is the
    address itself).
    """
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    if not claims.get("sub"):
        raise ValueError("missing authenticated user")
    return claims


def _query_params(event) -> dict:
    # Parse rawQueryString directly rather than trust the event's own
    # flattened queryStringParameters: more predictable for repeated keys.
    raw = event.get("rawQueryString") or ""
    parsed = parse_qs(raw, keep_blank_values=True)
    return {k: v[-1] for k, v in parsed.items()}


def _response(status: int, body: str):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": body,
    }


def _int_param(params: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer")
    return max(lo, min(hi, v))


# /jobs

def route_jobs(params: dict) -> dict:
    conn = get_connection()

    where_sql, args = build_jobs_where(params)

    sort_key = params.get("sort", "age")
    if sort_key not in SORT_COLUMNS:
        raise ValueError(f"sort must be one of: {', '.join(SORT_COLUMNS)}")
    sort_col = SORT_COLUMNS[sort_key]
    sort_dir = "DESC" if params.get("dir", "asc").lower() == "desc" else "ASC"
    # age and posted_at run in opposite directions: a lower age means a
    # more recent posted_at, so "age ASC" (default, newest first) needs
    # posted_at DESC. Flip only for this column.
    if sort_key == "age":
        sort_dir = "ASC" if sort_dir == "DESC" else "DESC"
    # NULLS LAST regardless of direction: SQLite treats NULL as smaller
    # than everything else, which would put it first on an ASC sort. The
    # non-age branch needs a genuine no-op constant, not a bare "0":
    # SQLite reads a bare integer literal in ORDER BY as a 1-indexed
    # column-position reference, and "0" is out of range there.
    null_order = "posted_at IS NULL" if sort_key == "age" else "NULL"

    limit = _int_param(params, "limit", default=100, lo=1, hi=500)
    offset = _int_param(params, "offset", default=0, lo=0, hi=10_000_000)

    total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_sql}", args).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT id, company_domain, ats, title, location, department, seniority, workplace_type, url,
               posted_at, confidence, first_seen, last_seen, closed_at,
               skills, salary_text, salary_is_estimate
        FROM jobs
        WHERE {where_sql}
        ORDER BY {null_order}, {sort_col} {sort_dir}
        LIMIT ? OFFSET ?
        """,
        [*args, limit, offset],
    ).fetchall()

    return {
        "jobs": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# /jobs/{id}: a stable permalink, separate from job.url (which the ATS
# can 404 once a role closes). Always resolves, answering with closed_at
# set if the job has closed, so a saved link never just dead-ends.

def route_job_detail(job_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT id, company_domain, ats, external_id, title, location, department, seniority,
               workplace_type, url, posted_at, description, confidence, first_seen, last_seen, closed_at
        FROM jobs WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


# /health

def route_health() -> dict:
    """Confirms the DB is actually reachable and reports pipeline
    freshness, not just "the Lambda is running." A 200 with ok=True here
    only means the process started; the real liveness signal is whether
    the query below succeeds and how old last_checked is.
    """
    conn = get_connection()
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM jobs) AS jobs_total,
          (SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL) AS jobs_open,
          (SELECT COUNT(*) FROM companies WHERE ats IS NOT NULL) AS companies_resolved,
          (SELECT MAX(last_checked) FROM companies) AS last_checked
        """
    ).fetchone()
    minutes_since_check = None
    if row["last_checked"] is not None:
        mins = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 1440.0 AS mins", (row["last_checked"],)
        ).fetchone()["mins"]
        if mins is not None:
            minutes_since_check = round(mins, 1)
    return {
        "ok": True,
        "db_reachable": True,
        "jobs_total": row["jobs_total"],
        "jobs_open": row["jobs_open"],
        "companies_resolved": row["companies_resolved"],
        "last_checked": row["last_checked"],
        "minutes_since_check": minutes_since_check,
    }


# /companies

def route_companies(params: dict) -> dict:
    conn = get_connection()
    where = ["1=1"]
    args: list = []

    if bool_param(params, "resolved_only"):
        where.append("ats IS NOT NULL")

    if params.get("ats"):
        ats_list = [a.strip() for a in params["ats"].split(",") if a.strip()]
        where.append("ats IN (%s)" % ",".join("?" * len(ats_list)))
        args.extend(ats_list)

    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"""
        SELECT domain, ats, token, confidence, job_count, tried, error, first_seen, last_checked
        FROM companies
        WHERE {where_sql}
        ORDER BY domain ASC
        """,
        args,
    ).fetchall()
    return {"companies": [dict(r) for r in rows], "total": len(rows)}


# /stats

def route_stats(params: dict | None = None) -> dict:
    """Everything the homepage dashboard needs, as a handful of cheap SQL
    aggregates. All "since" comparisons use julianday() diffs rather than
    string comparison, since ISO8601-with-offset and datetime('now')'s
    format don't sort reliably against each other at day boundaries.

    params only reads israel_only, which scopes top_locations to IL-tagged
    postings for the frontend's Location filter. Every other field stays
    global and independent of the job board's own local filters.
    """
    params = params or {}
    conn = get_connection()
    meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}

    by_ats = conn.execute(
        f"""
        SELECT ats, COUNT(*) AS n FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
        GROUP BY ats ORDER BY n DESC
        """
    ).fetchall()

    # companies_total/resolved describe scraper coverage, kept in the
    # response for API polling, not surfaced as a homepage metric.
    companies_total = int(meta.get("companies_total", 0))
    companies_resolved = int(meta.get("companies_resolved", 0))
    # The loader's raw all-time count (ghost listings included), kept as
    # open_jobs_all_time for transparency, but the headline number below
    # must respect the same archive cutoff as the board itself.
    open_jobs_all_time = int(meta.get("open_jobs_verified", 0))
    open_jobs_fresh, companies_hiring = conn.execute(
        f"""
        SELECT COUNT(*), COUNT(DISTINCT company_domain)
        FROM jobs WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
        """
    ).fetchone()

    last_checked = conn.execute("SELECT MAX(last_checked) AS latest FROM companies").fetchone()["latest"]
    minutes_since_update = None
    if last_checked is not None:
        mins = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 1440.0 AS mins", (last_checked,)
        ).fetchone()["mins"]
        if mins is not None:
            minutes_since_update = round(mins, 1)

    throughput = conn.execute(
        """
        SELECT
          SUM(CASE WHEN julianday('now') - julianday(first_seen) <= 1 THEN 1 ELSE 0 END) AS added_24h,
          SUM(CASE WHEN julianday('now') - julianday(first_seen) <= 7 THEN 1 ELSE 0 END) AS added_7d,
          SUM(CASE WHEN closed_at IS NOT NULL
                    AND julianday('now') - julianday(closed_at) <= 1 THEN 1 ELSE 0 END) AS closed_24h,
          SUM(CASE WHEN closed_at IS NOT NULL
                    AND julianday('now') - julianday(closed_at) <= 7 THEN 1 ELSE 0 END) AS closed_7d
        FROM jobs
        WHERE confidence = 'verified'
        """
    ).fetchone()

    # Verified only: best_effort postings don't carry a trustworthy posted_at.
    ages = sorted(
        r["d"] for r in conn.execute(
            f"""
            SELECT julianday('now') - julianday(posted_at) AS d
            FROM jobs
            WHERE closed_at IS NULL AND confidence = 'verified' AND posted_at IS NOT NULL AND {FRESH_CLAUSE}
            """
        ).fetchall()
    )
    n = len(ages)
    median_days = None
    if n:
        mid = n // 2
        median_days = ages[mid] if n % 2 else (ages[mid - 1] + ages[mid]) / 2

    # "Who's hiring" is the front-page question, not which ATS vendor a
    # listing came from (that's plumbing, not a market signal).
    top_companies = conn.execute(
        f"""
        SELECT company_domain AS domain, COUNT(*) AS n
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
        GROUP BY company_domain
        ORDER BY n DESC
        LIMIT 10
        """
    ).fetchall()

    # `department` is the raw ATS field, not a normalized taxonomy (one
    # company's "R&D" is another's "Engineering"). Shown to users as
    # "Category". LIMIT 20 doubles as the frontend's Category filter
    # options, not just this panel's display, hence the higher count.
    top_departments = conn.execute(
        f"""
        SELECT department, COUNT(*) AS n
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
          AND department IS NOT NULL AND TRIM(department) != ''
        GROUP BY department
        ORDER BY n DESC
        LIMIT 20
        """
    ).fetchall()

    # `location` is raw ATS text, not a normalized place. "Austin" and
    # "Austin, TX" are different rows here, not merged. A top-N of literal
    # strings, not a geocoded facet. Same IL_KEYWORDS match as route_jobs'
    # israel_only: keep in sync, don't invent a second heuristic.
    il_clause = " OR ".join("LOWER(location) LIKE ?" for _ in IL_KEYWORDS)
    il_args = [f"%{kw}%" for kw in IL_KEYWORDS]

    top_locations = conn.execute(
        f"""
        SELECT location, COUNT(*) AS n
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
          AND location IS NOT NULL AND TRIM(location) != ''
          {f"AND ({il_clause})" if bool_param(params, "israel_only") else ""}
        GROUP BY location
        ORDER BY n DESC
        LIMIT 40
        """,
        il_args if bool_param(params, "israel_only") else [],
    ).fetchall()

    location_row = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN {il_clause} THEN 1 ELSE 0 END) AS israel,
          COUNT(*) AS total
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
        """,
        il_args,
    ).fetchone()
    israel_count = location_row["israel"] or 0
    location_total = location_row["total"] or 0

    # Hiring velocity: who's added the most open reqs in the last week.
    # Different question from top_companies (total open headcount).
    # This surfaces a company ramping up right now even if its absolute
    # req count is still small.
    top_movers = conn.execute(
        f"""
        SELECT company_domain AS domain, COUNT(*) AS n
        FROM jobs
        WHERE confidence = 'verified' AND julianday('now') - julianday(first_seen) <= 7 AND {FRESH_CLAUSE}
        GROUP BY company_domain
        ORDER BY n DESC
        LIMIT 5
        """
    ).fetchall()

    # Excludes NULL. Most postings state no level, and an "unspecified"
    # bar would bury the real signal. The frontend derives that percentage
    # itself from totals.open_jobs minus this list's sum.
    seniority_breakdown = conn.execute(
        f"""
        SELECT seniority, COUNT(*) AS n
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE} AND seniority IS NOT NULL
        GROUP BY seniority
        ORDER BY n DESC
        """
    ).fetchall()

    # "Ghost job" signal: reuses `ages`, already computed above.
    # threshold_days is a response field, not a frontend assumption, so
    # changing it here needs no matching frontend edit.
    GHOST_THRESHOLD_DAYS = 60
    dormant_count = sum(1 for a in ages if a > GHOST_THRESHOLD_DAYS)

    # error_count: domains currently failing to resolve at all.
    # oldest_resolved_check: is the slowest part of the pipeline still
    # healthy. Distinct from freshness.last_checked below, which only
    # reflects the single most-recent company and would miss a straggler.
    pipeline_row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS error_count,
          MIN(CASE WHEN ats IS NOT NULL THEN last_checked END) AS oldest_resolved_check
        FROM companies
        """
    ).fetchone()
    oldest_resolved_check = pipeline_row["oldest_resolved_check"]
    oldest_check_minutes = None
    if oldest_resolved_check is not None:
        mins = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 1440.0 AS mins", (oldest_resolved_check,)
        ).fetchone()["mins"]
        if mins is not None:
            oldest_check_minutes = round(mins, 1)

    # New verified listings per day, last 14 days, zero-filled so the
    # frontend's chart gets a consistent 14-point series. Closed uses the
    # same shape/window from closed_at: the chart's other half.
    today = datetime.now(timezone.utc).date()
    window = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]

    new_rows = conn.execute(
        """
        SELECT date(first_seen) AS d, COUNT(*) AS n
        FROM jobs
        WHERE confidence = 'verified' AND julianday('now') - julianday(first_seen) <= 14
        GROUP BY d
        """
    ).fetchall()
    closed_rows = conn.execute(
        """
        SELECT date(closed_at) AS d, COUNT(*) AS n
        FROM jobs
        WHERE confidence = 'verified' AND closed_at IS NOT NULL
          AND julianday('now') - julianday(closed_at) <= 14
        GROUP BY d
        """
    ).fetchall()
    new_counts = {r["d"]: r["n"] for r in new_rows}
    closed_counts = {r["d"]: r["n"] for r in closed_rows}
    daily_new_jobs = [
        {"date": day, "n": new_counts.get(day, 0), "closed": closed_counts.get(day, 0)} for day in window
    ]

    # Open-jobs-over-time, reconstructed rather than snapshotted. This DB
    # has no periodic-snapshot mechanism, but nothing is ever deleted (jobs
    # get closed_at set, not removed), so "was this job open on day X" is
    # answerable from first_seen/closed_at alone. Done in Python, not SQL,
    # since it reads far more clearly as a loop than as a CTE.
    lifecycle_rows = conn.execute(
        "SELECT date(first_seen) AS fs, date(closed_at) AS ca FROM jobs WHERE confidence = 'verified'"
    ).fetchall()
    open_jobs_history = [
        {
            "date": day,
            "n": sum(1 for r in lifecycle_rows if r["fs"] and r["fs"] <= day and (r["ca"] is None or r["ca"] > day)),
        }
        for day in window
    ]

    return {
        "meta": meta,
        "open_jobs_by_ats": [dict(r) for r in by_ats],
        "top_companies": [dict(r) for r in top_companies],
        "top_departments": [dict(r) for r in top_departments],
        "top_locations": [dict(r) for r in top_locations],
        "top_movers_7d": [dict(r) for r in top_movers],
        "daily_new_jobs": daily_new_jobs,
        "open_jobs_history": open_jobs_history,
        "seniority_breakdown": [dict(r) for r in seniority_breakdown],
        "ghost": {
            "threshold_days": GHOST_THRESHOLD_DAYS,
            "dormant_count": dormant_count,
            "dormant_pct": round(dormant_count / n, 4) if n else 0,
            "sample_size": n,
        },
        "pipeline": {
            "error_count": pipeline_row["error_count"] or 0,
            "oldest_resolved_check_minutes": oldest_check_minutes,
        },
        "location": {
            "israel": israel_count,
            "other": location_total - israel_count,
            "total": location_total,
        },
        "totals": {
            "open_jobs": open_jobs_fresh,
            "open_jobs_all_time": open_jobs_all_time,
            "companies_hiring": companies_hiring,
            "companies_total": companies_total,
            "companies_resolved": companies_resolved,
            "resolution_rate": round(companies_resolved / companies_total, 4) if companies_total else 0,
        },
        "freshness": {
            "last_checked": last_checked,
            "minutes_since_update": minutes_since_update,
        },
        "throughput": {
            "new_jobs_24h": throughput["added_24h"] or 0,
            "new_jobs_7d": throughput["added_7d"] or 0,
            "closed_jobs_24h": throughput["closed_24h"] or 0,
            "closed_jobs_7d": throughput["closed_7d"] or 0,
        },
        "age": {
            "avg_open_days": round(sum(ages) / n, 1) if n else None,
            "median_open_days": round(median_days, 1) if median_days is not None else None,
            "oldest_open_days": round(ages[-1], 1) if n else None,
        },
    }


# /me/alerts: the one write surface on this whole API. Cognito-JWT-gated
# at the API Gateway layer (infra/apigateway.tf), not just in application
# code -- an unauthenticated request never reaches this Lambda for these
# routes at all. Backed by DynamoDB, not jobs.db: a per-user, low-volume,
# write-heavy table has nothing in common with the read-only, batch-
# loaded job data, and putting it in the same SQLite file would mean
# every fast-poll re-upload of jobs.db could race a user's own write.
#
# alert item shape: {user_id (Cognito sub), alert_id (uuid4), filter (the
# same query-param dict /api/jobs accepts), created_at, active,
# last_notified_at}. alerts.py (the evaluator, running in the scrape-fast
# Lambda after each fast-poll) reads this same table and feeds `filter`
# straight into job_filters.build_jobs_where -- an alert matches exactly
# what its owner would see applying those same filters on the live board,
# not a second approximation of it.

def route_list_alerts(user_id: str) -> dict:
    resp = _alerts_table.query(KeyConditionExpression=Key("user_id").eq(user_id))
    return {"alerts": resp.get("Items", [])}


def route_create_alert(claims: dict, body: dict) -> dict:
    filter_params = body.get("filter")
    if not isinstance(filter_params, dict):
        raise ValueError("filter must be an object of the same query params /api/jobs accepts")
    unknown = set(filter_params) - _ALLOWED_FILTER_KEYS
    if unknown:
        raise ValueError(f"unknown filter key(s): {', '.join(sorted(unknown))}")
    email = claims.get("email")
    if not email:
        raise ValueError("account has no email on file")

    now = datetime.now(timezone.utc).isoformat()
    item = {
        "user_id": claims["sub"],
        "alert_id": str(uuid.uuid4()),
        "email": email,
        "filter": filter_params,
        "created_at": now,
        "active": True,
        # Not None: alerts.py (the evaluator) only ever looks forward of
        # this watermark, so seeding it at creation time rather than
        # leaving it empty means a brand-new alert's first check only
        # catches genuinely new postings from here on -- not every
        # already-open job that happened to match on day one.
        "last_notified_at": now,
    }
    _alerts_table.put_item(Item=item)
    return item


def route_update_alert(user_id: str, alert_id: str, body: dict) -> dict | None:
    """Pause/resume only -- not a general PATCH. Changing the filter
    itself is a delete-and-recreate from the caller's side, simpler than
    reconciling a partial update against an in-flight evaluator scan.
    """
    if "active" not in body:
        raise ValueError("body must include 'active': true or false")
    try:
        resp = _alerts_table.update_item(
            Key={"user_id": user_id, "alert_id": alert_id},
            UpdateExpression="SET active = :a",
            ConditionExpression="attribute_exists(alert_id)",
            ExpressionAttributeValues={":a": bool(body["active"])},
            ReturnValues="ALL_NEW",
        )
    except _alerts_table.meta.client.exceptions.ConditionalCheckFailedException:
        return None
    return resp["Attributes"]


def route_delete_alert(user_id: str, alert_id: str) -> None:
    # No existence check: DELETE is idempotent by convention here, same
    # as a second delete of an already-deleted resource being a no-op
    # rather than an error.
    _alerts_table.delete_item(Key={"user_id": user_id, "alert_id": alert_id})
