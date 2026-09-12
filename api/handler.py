"""
OpenTechJobs API. One Lambda behind CloudFront (/api/* routes here, see
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
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import boto3
from boto3.dynamodb.conditions import Key

from aggregates import compute_facets, compute_stats
from db import get_connection
from help_page import HELP_HTML
from profile import (PROFILE_ID, SENIORITY, SKILLS, WORKPLACE, clean_profile,
                     empty_profile)
from skills import SKILL_TERMS
from job_filters import (FRESH_CLAUSE, IL_KEYWORDS, bool_param, build_jobs_where,
                         has_fts_index, has_places, salary_source_select,
                         skills_score_sql, wanted_skills)

_alerts_table = boto3.resource("dynamodb").Table(os.environ["ALERTS_TABLE"])

# What build_jobs_where() actually reads -- rejecting anything else at
# creation time catches a typo'd filter key immediately instead of it
# silently matching nothing forever, since the evaluator (alerts.py)
# just feeds this same dict straight into that same function.
_ALLOWED_FILTER_KEYS = {
    "search", "q", "keywords", "ats", "company", "department", "seniority", "location", "country",
    "city", "workplace", "confidence", "israel_only", "include_closed", "include_outdated",
    "min_age_days", "max_age_days", "skills",
}

# How long CloudFront may serve a cached answer, as distinct from how
# long a browser may. Kept below the interval at which the underlying
# snapshot can change, so a reader never sees an answer older than the
# data could be.
EDGE_CACHE_SECONDS = 180

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
        if path == "/help":
            # The one non-JSON route this Lambda serves -- see
            # help_page.py's own docstring for why it lives here instead
            # of as a static frontend page. 3600s: this content only
            # changes on a deploy, not with the data underneath it.
            return _html_response(200, HELP_HTML, cache_seconds=3600)
        if path == "/jobs":
            return _response(200, json.dumps(route_jobs(params), default=str), cache_seconds=60)
        if path.startswith("/jobs/") and len(path) > len("/jobs/"):
            job = route_job_detail(path[len("/jobs/"):])
            if job is None:
                return _response(404, json.dumps({"error": "no job with that id"}))
            return _response(200, json.dumps(job, default=str), cache_seconds=60)
        if path == "/companies":
            return _response(200, json.dumps(route_companies(params), default=str), cache_seconds=60)
        if path == "/stats":
            return _response(200, json.dumps(route_stats(params), default=str), cache_seconds=60)
        if path == "/facets":
            return _response(200, json.dumps(route_facets(params), default=str), cache_seconds=60)
        if path == "/health":
            return _response(200, json.dumps(route_health(), default=str), cache_seconds=60)
        if path == "/pipeline-status":
            return _response(200, json.dumps(route_pipeline_status(), default=str))
        if path == "/geo":
            # No cache_seconds, deliberately: the answer is per-viewer,
            # and CloudFront's own /api/geo behavior disables caching
            # for the same reason (infra/cloudfront.tf).
            return _response(200, json.dumps(route_geo(event)))
        if path == "/me/profile":
            claims = _authenticated_claims(event)
            if method == "GET":
                return _response(200, json.dumps(route_get_profile(claims["sub"]), default=str))
            if method == "PUT":
                body = json.loads(event.get("body") or "{}")
                return _response(200, json.dumps(route_put_profile(claims["sub"], body), default=str))
            return _response(405, json.dumps({"error": "method not allowed"}))
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


def _response(status: int, body: str, cache_seconds: int | None = None):
    # cache_seconds reaches the actual BROWSER's own HTTP cache -- CloudFront
    # already caches these paths for 120s at the edge (infra/cloudfront.tf's
    # own cache policy) regardless of this header, but confirmed live
    # (2026-09-08) that policy never forwarded a Cache-Control to the
    # client, so every page load meant a fresh network round-trip even
    # within CloudFront's own freshness window. 60s (matching db.py's own
    # S3_RECHECK_SECONDS, so the browser's cache window tracks how fresh
    # the underlying data actually could be) turns a revisit inside that
    # window into an instant from-disk response, no network at all.
    # Explicitly opt-in, not a blanket default: /pipeline-status exists
    # specifically to show whether a sync is happening RIGHT NOW, and the
    # /me/* alert routes are per-user and must never be shared/cached.
    #
    # s-maxage is the edge's own window and browsers ignore it, so the two
    # can differ. The comment above was written believing CloudFront
    # cached for 120s whatever this header said; it does not, it honours
    # the origin, so max-age=60 was also pinning the edge at 60 and every
    # minute meant a fresh Lambda invocation per edge per URL. The API was
    # the largest line on the bill at roughly 20,000 GB-seconds a day.
    #
    # 180s costs nothing real: the snapshot behind these answers changes
    # when the applier runs, which after batching is every five to nine
    # minutes, so a three-minute edge window is still well inside how
    # often the data itself can move.
    headers = {"Content-Type": "application/json", **CORS_HEADERS}
    if cache_seconds is not None:
        headers["Cache-Control"] = f"public, max-age={cache_seconds}, s-maxage={EDGE_CACHE_SECONDS}"
    return {
        "statusCode": status,
        "headers": headers,
        "body": body,
    }


def _html_response(status: int, body: str, cache_seconds: int | None = None):
    """Same shape as _response, just text/html -- only /help needs this;
    every other route on this Lambda answers JSON.
    """
    headers = {"Content-Type": "text/html; charset=utf-8", **CORS_HEADERS}
    if cache_seconds is not None:
        headers["Cache-Control"] = f"public, max-age={cache_seconds}"
    return {
        "statusCode": status,
        "headers": headers,
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

def _has_company_name(conn) -> bool:
    """Whether the snapshot in hand carries companies.company_name.

    Never assume it does. This Lambda's code and the database it reads
    are deployed on completely separate clocks: code ships in seconds via
    deploy-api.yml, while jobs-read.db only gains a new column when the
    merge next rebuilds it, up to an hour later. Referencing the column
    unconditionally took /api/jobs down with "no such column:
    company_name" for exactly that window, confirmed live. Degrading to
    NULL instead means the board shows domains for one merge cycle rather
    than 500ing, and a rollback of the loader can't break the API either.
    """
    return _has_company_column(conn, "company_name")


def _has_company_column(conn, name: str) -> bool:
    """Generalised from the above, for logo_url, which arrives the same
    way and would take the API down the same way if assumed.
    """
    try:
        return any(r[1] == name for r in conn.execute("PRAGMA table_info(companies)"))
    except Exception:
        return False


def route_jobs(params: dict) -> dict:
    conn = get_connection()

    company_name_select = (
        "(SELECT company_name FROM companies WHERE domain = jobs.company_domain) AS company_name"
        if _has_company_name(conn) else "NULL AS company_name"
    )
    # Same deploy-skew guard as company_name above: the column only
    # exists once the merge has rebuilt jobs-read.db with it.
    logo_select = (
        "(SELECT logo_url FROM companies WHERE domain = jobs.company_domain) AS logo_url"
        if _has_company_column(conn, "logo_url") else "NULL AS logo_url"
    )

    where_sql, args = build_jobs_where(params, has_fts_index(conn), has_places(conn))

    # The CV match. build_jobs_where has already narrowed the list to
    # rows carrying at least one of these; this counts how many, so the
    # board can lead with the closest fit rather than the newest one.
    wanted = wanted_skills(params)
    score_sql, score_args = skills_score_sql(wanted)

    sort_key = params.get("sort", "age")
    # "match" is a real sort key, not a default that applies when nobody
    # asked for one. The board always sends an explicit sort, so a
    # server-side default could never reach it, and ranked results came
    # back in date order with the ranking silently discarded.
    if sort_key == "match" and not wanted:
        sort_key = "age"
    if sort_key not in SORT_COLUMNS and sort_key != "match":
        raise ValueError(f"sort must be one of: {', '.join(SORT_COLUMNS)}, match")
    sort_col = SORT_COLUMNS.get(sort_key, SORT_COLUMNS["age"])
    sort_dir = "DESC" if params.get("dir", "asc").lower() == "desc" else "ASC"
    # age and posted_at run in opposite directions: a lower age means a
    # more recent posted_at, so "age ASC" (default, newest first) needs
    # posted_at DESC. Flip only for this column.
    # "match" shares this: inside a band of equally-good matches the
    # rows are read newest first, same as everywhere else on the board.
    if sort_key in ("age", "match"):
        sort_dir = "ASC" if sort_dir == "DESC" else "DESC"
    # NULLS LAST regardless of direction: SQLite treats NULL as smaller
    # than everything else, which would put it first on an ASC sort. The
    # non-age branch needs a genuine no-op constant, not a bare "0":
    # SQLite reads a bare integer literal in ORDER BY as a 1-indexed
    # column-position reference, and "0" is out of range there.
    null_order = "posted_at IS NULL" if sort_key in ("age", "match") else "NULL"
    # datetime() belongs to the date column alone. It used to wrap every
    # sort column, and datetime('Senior Backend Engineer') is NULL, so
    # every row tied and the board came back in scan order. Sorting by
    # title silently did nothing on the live site until a test asked it
    # to put three rows in alphabetical order and it refused.
    #
    # datetime() on posted_at is not decoration: the column is TEXT, and
    # rows written before _normalize_date() started forcing UTC (see
    # probe.py) carry other offsets, which a lexicographic sort gets
    # wrong even though each row's own age is right. NOCASE on the text
    # columns so "adobe" and "Adobe" are not two separate alphabets.
    # TRIM because a handful of ATSes serve titles with a leading space,
    # which otherwise sorts them above the letter A.
    sort_expr = (f"datetime({sort_col})" if sort_key in ("age", "match")
                 else f"TRIM({sort_col}) COLLATE NOCASE")
    order_sql = f"{null_order}, {sort_expr} {sort_dir}"
    order_args: list = []
    # Overlap first, date second. The ten closest fits are the whole
    # point of asking for a match, and any other order scatters them
    # through two thousand rows.
    if sort_key == "match":
        order_sql = f"{score_sql} DESC, {order_sql}"
        order_args = list(score_args)

    limit = _int_param(params, "limit", default=100, lo=1, hi=500)
    offset = _int_param(params, "offset", default=0, lo=0, hi=10_000_000)

    total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_sql}", args).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT id, company_domain, ats, title, location, department,
               category_of(department, title) AS category, seniority, workplace_type, url,
               posted_at, confidence, first_seen, last_seen, closed_at,
               skills, salary_text, salary_is_estimate, {salary_source_select(conn)},
               -- The company's own name as its ATS reports it.
               -- company_domain is often a hostname discovery guessed and
               -- never verified (see resolve_company_names.py), so this is
               -- what belongs anywhere a human reads it. NULL for ATSes
               -- that expose no name (Lever, Workday), and the UI falls
               -- back to the domain.
               --
               -- A scalar subquery rather than a LEFT JOIN, deliberately:
               -- jobs and companies share ats, confidence and first_seen,
               -- and build_jobs_where emits bare unqualified column names
               -- because it is shared with the alert evaluator, which
               -- queries jobs on its own. Joining would make every one of
               -- those filters ambiguous and error the whole route out.
               {company_name_select},
               {logo_select},
               {score_sql} AS match_score
        FROM jobs
        WHERE {where_sql}
        -- See sort_expr above for why the date column is wrapped and the
        -- text ones are not.
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
        """,
        # In the order SQLite binds them: the SELECT's score expression,
        # then WHERE, then the same expression again in ORDER BY.
        [*score_args, *args, *order_args, limit, offset],
    ).fetchall()

    return {
        "jobs": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        # Echoed back so the board can mark which chips on a row are the
        # ones that put it there, and can say what it is matching on
        # without re-parsing the URL it was handed.
        "matched_skills": wanted,
    }


# /jobs/{id}: a stable permalink, separate from job.url (which the ATS
# can 404 once a role closes). Always resolves, answering with closed_at
# set if the job has closed, so a saved link never just dead-ends.

def _description_from_s3(job_id: str) -> str | None:
    """The job's description blob, or None for anything unusable.

    Silent on failure on purpose: this is one of two places the text can
    live, and the caller still has the column. A missing blob is the
    normal state for every job written before this shipped.
    """
    bucket = os.environ.get("DATA_BUCKET")
    if not bucket:
        return None
    try:
        body = _status_s3.get_object(Bucket=bucket, Key=f"descriptions/{job_id}.json")["Body"].read()
        return json.loads(body).get("description") or None
    except Exception:
        return None


def route_job_detail(job_id: str) -> dict | None:
    """One listing, including its full description.

    The description is read from its own S3 object first and falls back
    to the column. Descriptions are ~94% of jobs-read.db's 1.2GB, and
    every reader of that file has to fit it in Lambda's 10GB /tmp, so
    they are moving out to make the snapshot small enough to rebuild
    often and cheap enough to pull inside a request. This route's own
    response shape does not change: /api/jobs/{id} is a public surface
    (see PRODUCT.md) and callers should never have to know where the
    text is stored.

    S3 is tried first deliberately, even while the column is still
    populated, so the new path is exercised in production now rather
    than the first time the column goes away.
    """
    conn = get_connection()
    # Same two company columns the list route selects, and guarded the
    # same way. Without logo_url here the detail drawer had nothing to
    # render and fell back to the browser guessing an icon, so the same
    # company could show a resolved logo in one place and a lettered
    # square in the other. Reported live from a screenshot showing
    # exactly that, in reverse: the list had the resolved URL and it was
    # the resolved URL that was failing.
    company_name_select = (
        "(SELECT company_name FROM companies WHERE domain = jobs.company_domain) AS company_name"
        if _has_company_name(conn) else "NULL AS company_name"
    )
    logo_select = (
        "(SELECT logo_url FROM companies WHERE domain = jobs.company_domain) AS logo_url"
        if _has_company_column(conn, "logo_url") else "NULL AS logo_url"
    )
    row = conn.execute(
        f"""
        SELECT id, company_domain, ats, external_id, title, location, department,
               category_of(department, title) AS category, seniority,
               workplace_type, url, posted_at, description, confidence, first_seen, last_seen, closed_at,
               {company_name_select},
               {logo_select}
        FROM jobs WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    if not row:
        return None
    job = dict(row)
    blob = _description_from_s3(job_id)
    if blob:
        job["description"] = blob
    return job


# /health

_status_s3 = boto3.client("s3")


def _read_status(bucket: str, key: str, stale_minutes: float) -> dict:
    """Shared logic for reading a status.json-shaped file (phase/detail/at,
    written by _write_status in scrape_handler.py, scrape_workday_handler.py,
    or scrape_maintenance_handler.py) and flagging a stuck/orphaned
    non-idle write as stale rather than trusting it forever.
    """
    try:
        obj = _status_s3.get_object(Bucket=bucket, Key=key)
        status = json.loads(obj["Body"].read())
    except Exception:
        # No such file yet (first deploy of this feature), or S3
        # hiccuped -- "unknown" is honest here, not a fabricated phase.
        return {"phase": "unknown", "detail": "", "at": None}

    # A non-idle, non-error phase that's been sitting for a long time is
    # an orphaned write from a crashed/killed run, not one still actually
    # in progress.
    stale = False
    try:
        age_minutes = (datetime.now(timezone.utc) - datetime.fromisoformat(status["at"])).total_seconds() / 60
        stale = status.get("phase") not in ("idle", "error") and age_minutes > stale_minutes
    except (KeyError, ValueError, TypeError):
        pass
    if stale:
        return {**status, "phase": "unknown", "detail": "last status update is stale"}
    return status


def route_pipeline_status() -> dict:
    """What the scrape pipeline is actually doing right now -- both the
    scrape side (scraping/loading/sending alerts/idle/error, status.json,
    written by scrape_handler.py and scrape_workday_handler.py) and the
    merge side (merging/idle/error, merge-status.json, written by
    scrape_maintenance_handler.py) -- not just "when was jobs-read.db
    last updated," which says nothing about whether a run is even in
    progress. Reported live: "syncing..." with a countdown reads as
    "something might be happening" regardless of whether anything
    actually is.

    Two separate keys/staleness thresholds, not one shared file: the
    scrape side runs every 5-10 minutes; the merge side runs hourly.
    Sharing one file would mean the merge's own "merging" phase gets
    overwritten by the very next fast-poll write within seconds -- the
    exact scenario the staleness check below exists to catch for a
    genuinely crashed run, just happening on nearly every real merge
    instead of only on a crash.
    """
    bucket = os.environ.get("DATA_BUCKET")
    if not bucket:
        empty = {"phase": "unknown", "detail": "", "at": None}
        return {"scrape": {**empty, "run": None}, "merge": empty}
    return {
        # Both pipelines finish well under this in practice (the
        # fast-poll in under a minute, discover in ~20).
        "scrape": _read_status(bucket, "status.json", stale_minutes=30),
        # A real merge finishes in well under 2 minutes -- 15 is generous
        # headroom, not a guess at the actual duration.
        "merge": _read_status(bucket, "merge-status.json", stale_minutes=15),
    }


def route_geo(event: dict) -> dict:
    """The viewer's own country, for the frontend to offer a local
    default without imposing one. Cloudflare's CF-IPCountry first,
    CloudFront's CloudFront-Viewer-Country second: while the zone is
    proxied through Cloudflare, CloudFront only ever sees Cloudflare's
    own IP, so ITS header reports whichever Cloudflare PoP took the
    request, not where the person actually is. Neither header reaches
    this Lambda unless /api/geo's own cache behavior forwards it --
    /api/* strips every header (see infra/cloudfront.tf).

    Answers null rather than guessing. An absent or unusable value
    means the frontend shows no prompt at all, which is the right
    failure: a wrong country guess is worse than none, and this is
    only ever a suggestion the visitor can ignore.
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    for key in ("cf-ipcountry", "cloudfront-viewer-country"):
        raw = (headers.get(key) or "").strip().upper()
        # XX is Cloudflare's own "couldn't tell", T1 is Tor. Both are
        # real values it sends, neither is a country.
        if len(raw) == 2 and raw.isalpha() and raw not in ("XX", "T1"):
            return {"country": raw, "source": key}
    return {"country": None, "source": None}


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
    # Set by load_to_sqlite.py's check_timestamp_clustering (every load,
    # any source) -- the real signature both the gloat.com Comeet bug and
    # the Workday "Posted Today" bug shared: many jobs across DIFFERENT
    # companies stamped with the exact identical posted_at. Empty string
    # (not missing) once any load has run -- "" is a real, checked-and-
    # clean result, not "never checked."
    clustering_row = conn.execute(
        "SELECT value FROM meta WHERE key = 'timestamp_clustering_warnings'"
    ).fetchone()
    clustering_warnings = clustering_row["value"].split("; ") if clustering_row and clustering_row["value"] else []
    return {
        "ok": True,
        "db_reachable": True,
        "jobs_total": row["jobs_total"],
        "jobs_open": row["jobs_open"],
        "companies_resolved": row["companies_resolved"],
        "last_checked": row["last_checked"],
        "minutes_since_check": minutes_since_check,
        "timestamp_clustering_warnings": clustering_warnings,
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

# /facets: per-option counts for the board's own filter dropdowns
# (Category, Location, Company), scoped to whatever ELSE is currently
# selected. Reported live: picking "Security" showed 369 (every open
# Security role anywhere), and picking Israel-only on top of it still
# showed 369 in the dropdown even though the board itself dropped to 98
# -- the dropdown counts came from /stats' top_departments/top_locations,
# which are deliberately global (that endpoint's own docstring: "every
# other field stays global," true for the Market Stats dashboard, wrong
# for a filter option's own count). Standard faceted-search convention:
# an option's count answers "how many would I see if I ALSO picked
# this," so it's computed with every OTHER currently active filter
# applied but that option's OWN filter key excluded -- picking Security
# doesn't need to already be selected to see its current count, and
# selecting it shouldn't make its own count self-referential.
# Precomputed aggregates, written by the applier right after it pushes a
# snapshot (loader/precompute.py). Cached per container on the same 60s
# clock db.py uses for the snapshot itself, so a request pays at most one
# S3 read a minute and usually none.
_PRECOMPUTED_TTL = 60.0
_precomputed: dict[str, tuple[float, dict | None]] = {}


def _precomputed_json(name: str) -> dict | None:
    """The applier's answer, or None meaning compute it here instead.

    None is not an error. It is the normal state for the window between
    this code deploying and the next merge producing the file, and for
    any run where writing it failed. Code ships in seconds and the
    snapshot only changes when the merge next runs: assuming the new
    thing is already there has taken this API down before, so the live
    path stays reachable rather than becoming dead code.
    """
    now = time.monotonic()
    cached = _precomputed.get(name)
    if cached is not None and now - cached[0] < _PRECOMPUTED_TTL:
        return cached[1]
    bucket = os.environ.get("DATA_BUCKET")
    value = None
    if bucket:
        try:
            body = _status_s3.get_object(Bucket=bucket, Key=f"precomputed/{name}")["Body"].read()
            value = json.loads(body)
        except Exception as e:
            print(f"precomputed/{name} unavailable, computing live: {e!r}")
    _precomputed[name] = (now, value)
    return value


def _live_freshness() -> tuple[dict, dict]:
    """The two clock-shaped fields of a stats response, read now.

    Cheap on purpose: MAX over an indexed column and a six-row table.
    The expensive part of /api/stats is the aggregates, which stay
    precomputed.
    """
    conn = get_connection()
    meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
    last_checked = conn.execute("SELECT MAX(last_checked) AS latest FROM companies").fetchone()["latest"]
    minutes = None
    if last_checked is not None:
        mins = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 1440.0 AS mins", (last_checked,)
        ).fetchone()["mins"]
        if mins is not None:
            minutes = round(mins, 1)
    return {"last_checked": last_checked, "minutes_since_update": minutes}, meta


def _unfiltered_confidence(params: dict) -> str | None:
    """The confidence variant this request wants, or None if it carries
    any other filter and so has no precomputed answer.

    Compares the generated WHERE rather than listing parameter names, so
    a filter added to build_jobs_where later cannot quietly start being
    served a precomputed answer that ignores it. Confidence is held
    constant on both sides and returned separately, because it is the
    one filter the board always sends: it defaults to "all" where the
    API defaults to "verified", and treating that as "filtered" meant
    the precomputed facets were never once used by the page they were
    built for.

    The FTS flag is constant on both sides too. It only changes the
    keywords branch, and a request with keywords is filtered regardless.
    """
    confidence = params.get("confidence") or "verified"
    probe = {**params, "confidence": "verified"}
    if build_jobs_where(probe, True) != build_jobs_where({"confidence": "verified"}, True):
        return None
    return confidence


def route_facets(params: dict) -> dict:
    # Facets are filter-dependent by design: each one is counted with
    # every OTHER active filter applied, so only the unfiltered case can
    # be precomputed. That is also the one every page load asks for.
    variant = _unfiltered_confidence(params)
    if variant is not None:
        ready = _precomputed_json("facets.json")
        # A snapshot written before this was keyed by confidence has the
        # three lists at the top level instead of a variant map. Falling
        # through is correct for it, and stops being needed one merge
        # after this ships.
        if isinstance(ready, dict) and variant in ready:
            return ready[variant]
    return compute_facets(get_connection(), params)


def route_stats(params: dict | None = None) -> dict:
    params = params or {}
    ready = _precomputed_json("stats.json")
    if ready is not None:
        # israel_only is the only thing that changes this response, and it
        # changes exactly one field, so both versions of that field ship
        # in one object rather than two near-identical files.
        out = {k: v for k, v in ready.items() if k != "top_locations_israel"}
        if bool_param(params, "israel_only"):
            out["top_locations"] = ready.get("top_locations_israel", out.get("top_locations", []))
        # Two fields in here are clocks, not aggregates, and freezing a
        # clock for fifteen minutes makes it wrong rather than stale.
        # Reported live: the Data Health tile read "19M old" while the
        # pipeline was four minutes behind, because the artifact carried
        # the timestamp from when it was built.
        #
        # Refreshed from the snapshot on every request. This is one
        # indexed MAX and a six-row table read, which is nothing like the
        # aggregates the artifact exists to avoid.
        try:
            out["freshness"], out["meta"] = _live_freshness()
        except Exception as e:
            print(f"couldn't refresh freshness on precomputed stats: {e!r}")
        return out
    return compute_stats(get_connection(), params)


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
    # The profile shares this partition under a sentinel sort key (see
    # profile.py), so it comes back from the same Query and would render
    # as an alert with no filter.
    items = [i for i in resp.get("Items", []) if i.get("alert_id") != PROFILE_ID]
    return {"alerts": items}


def route_get_profile(user_id: str) -> dict:
    """The caller's own profile, or an empty one.

    Absent is not an error: everyone has a profile conceptually, most
    have never filled one in, and a 404 would make the page handle a
    case that is really just "no skills yet".
    """
    resp = _alerts_table.get_item(Key={"user_id": user_id, "alert_id": PROFILE_ID})
    item = resp.get("Item") or {}
    stored = empty_profile()
    stored.update({k: item[k] for k in stored if k in item})
    # The vocabularies ship with the profile rather than from their own
    # route. The page needs both to render at all, and a second copy of
    # the skill list in the frontend is exactly the drift this project
    # already created once between probe.py and the API.
    return {
        "profile": stored,
        "options": {"skills": SKILLS, "seniority": SENIORITY, "workplace": WORKPLACE},
        # The needles as well as the labels, because the CV analyser runs
        # in the reader's own browser: the file is never uploaded, so the
        # matching has to happen there, which means the browser needs the
        # same terms probe.py tags jobs with. Not secret, and shipping
        # them is what keeps one vocabulary rather than two.
        "skill_terms": [{"label": label, "needles": needles} for label, needles in SKILL_TERMS],
    }


def route_put_profile(user_id: str, body: dict) -> dict:
    """Replace the caller's profile, validated down to known values.

    A whole-object PUT rather than a PATCH: a profile is small, the page
    always holds all of it, and merging partial updates would make
    "clear my skills" indistinguishable from "leave them alone".
    """
    cleaned = clean_profile(body)
    _alerts_table.put_item(Item={
        "user_id": user_id,
        "alert_id": PROFILE_ID,
        **cleaned,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"profile": cleaned}


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


def _alert_update_args(user_id: str, alert_id: str, body: dict) -> dict:
    """The update_item() call for a PATCH, built and validated without
    touching DynamoDB, so the expression itself is testable.
    """
    sets = {}
    if "active" in body:
        sets[":a"] = ("active = :a", bool(body["active"]))
    if "filter" in body:
        filter_params = body["filter"]
        if not isinstance(filter_params, dict):
            raise ValueError("filter must be an object of the same query params /api/jobs accepts")
        unknown = set(filter_params) - _ALLOWED_FILTER_KEYS
        if unknown:
            raise ValueError(f"unknown filter key(s): {', '.join(sorted(unknown))}")
        sets[":f"] = ("#f = :f", filter_params)
        sets[":n"] = ("last_notified_at = :n", datetime.now(timezone.utc).isoformat())
    if not sets:
        raise ValueError("body must include 'active' and/or 'filter'")

    args = {
        "Key": {"user_id": user_id, "alert_id": alert_id},
        "UpdateExpression": "SET " + ", ".join(expr for expr, _ in sets.values()),
        "ConditionExpression": "attribute_exists(alert_id)",
        "ExpressionAttributeValues": {k: v for k, (_, v) in sets.items()},
        "ReturnValues": "ALL_NEW",
    }
    # `filter` is a DynamoDB reserved word, so it can only appear in an
    # UpdateExpression behind a name placeholder. Passing the names map
    # when it is empty is itself an error, hence the conditional.
    if ":f" in sets:
        args["ExpressionAttributeNames"] = {"#f": "filter"}
    return args


def route_update_alert(user_id: str, alert_id: str, body: dict) -> dict | None:
    """Pause/resume, edit the filter, or both in one call.

    This used to be pause/resume only, on the grounds that a filter edit
    could race the evaluator mid-scan. It can, and the cost of losing
    that race is that one 5-minute cycle evaluates the old filter. That
    is a smaller problem than the one it created: the only way to change
    an alert was to delete it and build it again from scratch, which
    loses the alert_id, the created_at, and any chance of the owner
    recognising it in the list.

    A filter edit moves last_notified_at to now, for the same reason
    creation seeds it: widening an alert should start watching from here,
    not mail out every already-open job the new filter happens to match.
    """
    args = _alert_update_args(user_id, alert_id, body)
    try:
        resp = _alerts_table.update_item(**args)
    except _alerts_table.meta.client.exceptions.ConditionalCheckFailedException:
        return None
    return resp["Attributes"]


def route_delete_alert(user_id: str, alert_id: str) -> None:
    # No existence check: DELETE is idempotent by convention here, same
    # as a second delete of an already-deleted resource being a no-op
    # rather than an error.
    _alerts_table.delete_item(Key={"user_id": user_id, "alert_id": alert_id})
