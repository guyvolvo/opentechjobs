"""
IL/JOBS read API. One Lambda, invoked via a Function URL behind
CloudFront (see infra/cloudfront.tf -- /api/* routes here).

No framework: this is a handful of GET routes over a ~2MB SQLite file,
and a router built on if/elif reads exactly as clearly as one built on a
micro-framework here without the extra dependency/cold-start weight.

No write endpoints, on purpose: the brief's product has no accounts, so
there's no user identity to own a write. Job data is written only by the
batch loader (loader/load_to_sqlite.py, run from scrape.yml); "CRUD" for
an individual visitor -- marking a listing interesting/applied/hidden --
stays client-local (localStorage), same as the earlier static viewer,
because there's no login to hang server-side state off of.
"""

import json
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from db import get_connection

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

IL_KEYWORDS = [
    "israel", "tel aviv", "tel-aviv", "telaviv", "herzliya", "raanana", "ra'anana",
    "rehovot", "netanya", "haifa", "jerusalem", "beer sheva", "beersheva",
    "petah tikva", "yokneam", "kfar saba", "ramat gan", "modiin", "modi'in",
    "caesarea", "yavne", "hod hasharon", "bnei brak", "rosh haayin", "tlv",
    # Added after checking real (unmatched) location strings in the data
    # rather than guessing -- these are genuine misses, confirmed against
    # actual postings: "Givatayim", "Karmiel", "Kiryat Bialik", "Rishon
    # Le-Zion", "Yehud" were all sitting untagged. "kiryat" is deliberately
    # generic -- it's the Hebrew word for "town of" and catches every
    # Kiryat-prefixed city (Ono, Gat, Shmona, Motzkin, ...) in one entry
    # rather than enumerating each. Did NOT add "Azur" (a real Israeli
    # city) despite finding it -- "azur" is a substring of "Azure", and a
    # hybrid/remote posting mentioning the technology in its location
    # field is a more plausible false-positive than "Azur" the city is a
    # true positive, for one small town.
    "givatayim", "karmiel", "kiryat", "rishon", "yehud",
]

# A posting whose ATS-reported posted_at is more than this many days old is
# treated as an archived ghost listing, not a real open req -- ATSes don't
# reliably mark stale postings closed (one Lever listing in this dataset
# has posted_at from 2009), so age is the practical signal instead. Hidden
# from the board by default (see FRESH_CLAUSE / route_jobs's include_stale
# param) and excluded from every market-metric aggregate in route_stats --
# a 16-year-old "open" listing shouldn't be counted as one, or drag the
# oldest/median age stats into meaninglessness. NULL posted_at is kept,
# not hidden -- an unknown date isn't evidence of staleness.
BOARD_MAX_AGE_DAYS = 365
FRESH_CLAUSE = f"(posted_at IS NULL OR julianday('now') - julianday(posted_at) <= {BOARD_MAX_AGE_DAYS})"


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
            return _response(200, json.dumps({"ok": True}))
        return _response(404, json.dumps({"error": f"no route for {path}"}))
    except ValueError as e:
        return _response(400, json.dumps({"error": str(e)}))
    except Exception as e:  # last resort: never leak a raw traceback to callers
        return _response(500, json.dumps({"error": "internal error", "detail": str(e)}))


def _query_params(event) -> dict:
    # Function URL events give queryStringParameters as a flat dict
    # (comma-joined for repeated keys); parse_qs on rawQueryString is more
    # predictable for list-shaped params like ats=a,b vs ats=a&ats=b, so
    # just parse the raw string ourselves.
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


def _bool_param(params: dict, name: str) -> bool:
    return params.get(name, "").lower() in ("1", "true", "yes")


def _add_in_filter(where: list, args: list, params: dict, param_name: str, column: str) -> None:
    """?param=a,b,c -> `column IN (?,?,?)`. Shared by every multi-select
    filter (ats, company, department, seniority) -- the frontend's filter
    dropdowns are all multi-select, checking several roles/companies/
    levels at once is the normal case, not a single exact match.
    """
    raw = params.get(param_name)
    if not raw:
        return
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if not values:
        return
    where.append(f"{column} IN (%s)" % ",".join("?" * len(values)))
    args.extend(values)


# ---------------------------------------------------------------------------
# /jobs
# ---------------------------------------------------------------------------

def route_jobs(params: dict) -> dict:
    conn = get_connection()

    where = ["1=1"]
    args: list = []

    include_closed = _bool_param(params, "include_closed")
    if not include_closed:
        where.append("closed_at IS NULL")

    confidence = params.get("confidence", "verified")
    if confidence == "verified":
        where.append("confidence = 'verified'")
    elif confidence == "best_effort":
        where.append("confidence = 'best_effort'")
    elif confidence != "all":
        raise ValueError("confidence must be one of: verified, best_effort, all")

    _add_in_filter(where, args, params, "ats", "ats")
    _add_in_filter(where, args, params, "company", "company_domain")
    _add_in_filter(where, args, params, "department", "department")
    _add_in_filter(where, args, params, "seniority", "seniority")
    _add_in_filter(where, args, params, "location", "location")
    _add_in_filter(where, args, params, "workplace", "workplace_type")

    if not _bool_param(params, "include_stale"):
        where.append(FRESH_CLAUSE)

    if params.get("q"):
        q = f"%{params['q'].lower()}%"
        where.append(
            "(LOWER(title) LIKE ? OR LOWER(company_domain) LIKE ? OR LOWER(location) LIKE ? OR LOWER(department) LIKE ?)"
        )
        args.extend([q, q, q, q])

    if params.get("keywords"):
        # ';'-separated, ALL must appear (AND, not OR) -- "azure;excel;iso"
        # means "mentions Azure AND Excel AND ISO", narrowing toward a
        # specific combination rather than broadening to any one of them.
        # Matched against title OR description so a job still matches on
        # a keyword that's in the title even for the ATSes description is
        # None for (see db/schema.sql's note on which ones those are).
        for term in (t.strip() for t in params["keywords"].split(";")):
            if not term:
                continue
            like = f"%{term.lower()}%"
            where.append("(LOWER(title) LIKE ? OR LOWER(COALESCE(description, '')) LIKE ?)")
            args.extend([like, like])

    if _bool_param(params, "israel_only"):
        clauses = " OR ".join("LOWER(location) LIKE ?" for _ in IL_KEYWORDS)
        where.append(f"({clauses})")
        args.extend(f"%{kw}%" for kw in IL_KEYWORDS)

    min_age = params.get("min_age_days")
    if min_age:
        where.append("posted_at IS NOT NULL AND julianday('now') - julianday(posted_at) >= ?")
        args.append(int(min_age))

    max_age = params.get("max_age_days")
    if max_age:
        where.append("posted_at IS NOT NULL AND julianday('now') - julianday(posted_at) <= ?")
        args.append(int(max_age))

    sort_key = params.get("sort", "age")
    if sort_key not in SORT_COLUMNS:
        raise ValueError(f"sort must be one of: {', '.join(SORT_COLUMNS)}")
    sort_col = SORT_COLUMNS[sort_key]
    # Default is "asc" -- for a job board, "newest posting first" is the
    # obviously useful default, not an audit of the oldest ghost listings.
    sort_dir = "DESC" if params.get("dir", "asc").lower() == "desc" else "ASC"
    # age and posted_at run in opposite directions -- older posting date
    # means *higher* age, so "age" ascending (newest/lowest-age first, the
    # default) has to sort posted_at DESC, or it would silently return
    # oldest-first instead. Flip only for this column.
    if sort_key == "age":
        sort_dir = "ASC" if sort_dir == "DESC" else "DESC"
    # NULLS LAST regardless of direction -- rows with no posted_at sink to
    # the bottom either way, not jump to the top on an ASC sort just
    # because SQLite treats NULL as smaller than everything else.
    null_order = "posted_at IS NULL" if sort_key == "age" else "0"

    limit = _int_param(params, "limit", default=100, lo=1, hi=500)
    offset = _int_param(params, "offset", default=0, lo=0, hi=10_000_000)

    where_sql = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_sql}", args).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT id, company_domain, ats, title, location, department, seniority, workplace_type, url,
               posted_at, confidence, first_seen, last_seen, closed_at
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


# ---------------------------------------------------------------------------
# /jobs/{id} -- a stable, linkable/bookmarkable permalink for one posting.
# Deliberately separate from job.url (the link to the actual ATS listing,
# which the ATS can 404 once a role closes): this one always resolves and
# still answers with closed_at set, so a saved link keeps being useful --
# "is this still open" is itself an answer, not a dead link. That's the
# same "poll it yourself" premise as the rest of the API, just narrowed to
# a single job instead of a filtered list.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# /companies
# ---------------------------------------------------------------------------

def route_companies(params: dict) -> dict:
    conn = get_connection()
    where = ["1=1"]
    args: list = []

    if _bool_param(params, "resolved_only"):
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


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

def route_stats(params: dict | None = None) -> dict:
    """Everything the homepage's SRE-style dashboard needs, computed as a
    handful of cheap SQL aggregates over a DB that's a few MB -- no reason
    to pull rows into Python and reduce there except for the median (SQLite
    has no built-in percentile function), and even that's over a few
    thousand rows at most, not worth a second network round-trip to avoid.

    All "since" comparisons use julianday() diffs, not string comparison
    against datetime('now'), because first_seen/last_seen/closed_at are
    ISO8601 with a "+00:00" offset suffix (see loader.py's now_iso()) while
    datetime('now') emits "YYYY-MM-DD HH:MM:SS" -- those two formats sort
    correctly against each other most of the time but not at exact day
    boundaries, and julianday() parses both correctly regardless. Same
    pattern route_jobs() already uses for min_age_days/max_age_days.

    params is optional and today only reads israel_only, which scopes
    top_locations to IL-tagged postings -- so the frontend's Location
    filter can offer only Israeli options once the board's IL-only toggle
    is on, instead of showing 40 mostly-irrelevant global offices. Every
    other field in this response is deliberately NOT scoped by it: Market
    Stats is a global dashboard independent of the job board's local
    filters (see style.css's design-system note on the two being
    separate), and re-deriving open-job counts etc. under a param would
    make that dashboard's numbers move for a reason a visitor watching it
    wouldn't see cause.
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

    # companies_total/companies_resolved describe scraper coverage (we
    # tried N domains, resolved M of them) -- real numbers, kept below for
    # anyone polling the API who wants them, but not a homepage metric: a
    # visitor has no use for "how many companies IL/JOBS failed to
    # resolve," and displaying it just reads as an admission of gaps
    # rather than a market signal. companies_hiring (distinct companies
    # with a fresh open verified req) is the number that's actually
    # meaningful to show.
    companies_total = int(meta.get("companies_total", 0))
    companies_resolved = int(meta.get("companies_resolved", 0))
    # meta's open_jobs_verified is the loader's raw all-time count (every
    # non-closed verified job, ghost listings included) -- kept for
    # transparency as open_jobs_all_time below, but the headline number
    # has to respect the same archive cutoff as the board itself, or
    # "open jobs" and "what's actually on the board" would just disagree.
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

    # Open-job age distribution, verified only -- best_effort postings don't
    # carry a trustworthy posted_at (see confidence comment in db/schema.sql).
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

    # Which companies actually have open reqs right now -- "who's hiring"
    # is the question a job board's front page should answer, not "which
    # ATS vendor did we poll it from" (that's plumbing, not market signal).
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

    # `department` is the raw ATS field name (see db/schema.sql) -- not a
    # normalized taxonomy, one company's "R&D" is another's "Engineering"
    # -- but it reads to a job seeker as "what role track is this," so the
    # UI/API-facing label is "role", not "department". Same LIMIT doubles
    # as the options list for the frontend's role filter dropdown, not
    # just this panel's top-N display -- bumped past a display-only count
    # so that filter has enough real choices.
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

    # `location` is raw ATS text, not a normalized place -- "Austin",
    # "Austin, TX", and "Austin, Texas, United States" are three different
    # rows here, not one. This is a curated top-N of literal strings (same
    # tradeoff as top_departments above), not a real geocoded location
    # facet -- picking "Tel Aviv" won't also catch a posting that says
    # "Tel Aviv-Yafo, Israel" unless that exact string is also common
    # enough to make its own entry. The israel_only toggle (IL_KEYWORDS,
    # a curated substring match) is still the right tool for "just IL,"
    # not this -- this is for narrowing to a specific office once that's
    # not enough.
    # Same IL_KEYWORDS substring match route_jobs() uses for israel_only --
    # keep this in sync with that list, don't invent a second heuristic.
    il_clause = " OR ".join("LOWER(location) LIKE ?" for _ in IL_KEYWORDS)
    il_args = [f"%{kw}%" for kw in IL_KEYWORDS]

    top_locations = conn.execute(
        f"""
        SELECT location, COUNT(*) AS n
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
          AND location IS NOT NULL AND TRIM(location) != ''
          {f"AND ({il_clause})" if _bool_param(params, "israel_only") else ""}
        GROUP BY location
        ORDER BY n DESC
        LIMIT 40
        """,
        il_args if _bool_param(params, "israel_only") else [],
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
    # Different question from top_companies (total open headcount) --
    # this surfaces a company ramping up right now even if its absolute
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

    # Excludes NULL on purpose -- most postings state no level at all (see
    # Job.seniority's docstring in probe.py), and a bar chart dominated by
    # one "unspecified" bar would bury the actual signal. The frontend
    # derives "N% state no level" itself from totals.open_jobs minus the
    # sum of this list, rather than this needing its own field for that.
    seniority_breakdown = conn.execute(
        f"""
        SELECT seniority, COUNT(*) AS n
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE} AND seniority IS NOT NULL
        GROUP BY seniority
        ORDER BY n DESC
        """
    ).fetchall()

    # "Ghost job" signal -- reuses the `ages` list already computed above
    # for median/oldest, no extra query. threshold_days is deliberately a
    # response field, not a hardcoded frontend assumption, so changing it
    # here doesn't require a matching frontend edit.
    GHOST_THRESHOLD_DAYS = 60
    stale_count = sum(1 for a in ages if a > GHOST_THRESHOLD_DAYS)

    # Pipeline health: companies_resolved/total/resolution_rate already
    # exist under totals below -- this only adds what isn't there yet.
    # error_count is "how many domains are currently failing to resolve
    # at all," not "how many jobs failed" (that's a different thing this
    # DB doesn't track). oldest_resolved_check answers "is the slowest-
    # updated part of the pipeline still healthy," distinct from
    # freshness.last_checked below, which is only the single most-recent
    # company -- a stuck straggler wouldn't show up there at all.
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

    # New verified listings per day, last 14 days -- zero-filled so the
    # frontend's chart gets a consistent 14-point series instead of having
    # to guess which days SQL silently omitted for having no rows. Closed
    # is the same shape over the same window, from closed_at instead --
    # ingest vs egress, the two sides of the same "New Listings" chart.
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

    # Open-jobs-over-time, reconstructed rather than snapshotted -- this
    # DB is current-state-only (see db/schema.sql's header) and there's no
    # periodic-snapshot mechanism, but nothing is ever deleted (jobs get
    # closed_at set, not removed -- see loader.py), so first_seen/closed_at
    # on every job the pipeline has EVER seen is a real lifecycle log, not
    # just a point-in-time view. "Was this job open on day X" is answerable
    # after the fact from that alone: first_seen on or before X, and
    # either still open or not closed until after X. Done in Python, not
    # SQL, for the same reason the median-age calc above is -- a few
    # thousand rows times 14 days is trivial either way, and this reads
    # far more clearly as a loop than as a CTE.
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
            "stale_count": stale_count,
            "stale_pct": round(stale_count / n, 4) if n else 0,
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
