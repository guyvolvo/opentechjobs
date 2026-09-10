"""The two expensive aggregate routes, with the database passed in.

Both used to live in handler.py and open their own connection. They are
here because something other than the API now needs to run them: the
5-minute applier computes both against the snapshot it just built and
ships the answers as JSON, so a request can read a finished number
instead of deriving it.

Measured before that change, with the edge cache bypassed: /api/stats
took 7.03s and /api/facets 5.02s, against 0.44s for /api/jobs. Both are
fired on every page load. That was most of the API's whole compute bill,
and none of it was work that differs between one visitor and the next.

Nothing in here imports db.py, deliberately. That module builds an S3
client at import time, which is right for a Lambda serving requests and
wrong for a loader that already has a file open on disk.
"""

from datetime import datetime, timedelta, timezone

from job_filters import (FRESH_CLAUSE, IL_KEYWORDS, bool_param, build_jobs_where,
                         has_fts_index)


def compute_facets(conn, params: dict) -> dict:

    def counts_by(column_expr: str, exclude_param: str, limit: int) -> list[dict]:
        scoped = dict(params)
        scoped.pop(exclude_param, None)
        where_sql, args = build_jobs_where(scoped, has_fts_index(conn))
        rows = conn.execute(
            f"""
            SELECT {column_expr} AS value, COUNT(*) AS n
            FROM jobs
            WHERE {where_sql} AND {column_expr} IS NOT NULL AND TRIM({column_expr}) != ''
            GROUP BY {column_expr}
            ORDER BY n DESC
            LIMIT ?
            """,
            [*args, limit],
        ).fetchall()
        return [dict(r) for r in rows]

    return {
        "categories": counts_by("category_of(department, title)", "department", 20),
        "locations": counts_by("location", "location", 40),
        "companies": counts_by("company_domain", "company", 500),
    }


def compute_stats(conn, params: dict | None = None) -> dict:
    """Everything the homepage dashboard needs, as a handful of cheap SQL
    aggregates. All "since" comparisons use julianday() diffs rather than
    string comparison, since ISO8601-with-offset and datetime('now')'s
    format don't sort reliably against each other at day boundaries.

    params only reads israel_only, which scopes top_locations to IL-tagged
    postings for the frontend's Location filter. Every other field stays
    global and independent of the job board's own local filters.
    """
    params = params or {}
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

    # Shown to users as "Category". Used to be a GROUP BY on the raw
    # department column -- one company's "R&D" is another's
    # "Engineering", so that was really just "the top 20 raw strings by
    # count," not a real taxonomy. category_of() (job_filters.py,
    # registered on this connection by db.py) normalizes department+title
    # into a small fixed set instead (Security, Infrastructure, Software
    # Engineering, ...), same function build_jobs_where's "department"
    # filter now matches against, so what a user picks here is exactly
    # what they filter by. LIMIT 20 is moot now (<=len(CATEGORIES)
    # possible rows) but harmless to leave as a cap.
    # Grouped by category AND seniority in one pass, with the plain
    # category list derived from it below. category_of() is a Python
    # function called per row, and it is most of what makes this
    # function slow, so the cross-tab must not cost a second pass.
    category_seniority_rows = conn.execute(
        f"""
        SELECT category_of(department, title) AS category, seniority, COUNT(*) AS n
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
          AND category_of(department, title) IS NOT NULL
        GROUP BY category_of(department, title), seniority
        """
    ).fetchall()
    by_category: dict = {}
    for r in category_seniority_rows:
        by_category[r["category"]] = by_category.get(r["category"], 0) + r["n"]
    top_departments = [
        {"department": c, "n": n}
        for c, n in sorted(by_category.items(), key=lambda kv: -kv[1])[:20]
    ]
    # NULL seniority is most rows and is a real answer ("unstated"),
    # kept rather than dropped so the heatmap's row totals reconcile
    # with the category list.
    category_seniority = [
        {"category": r["category"], "seniority": r["seniority"] or "unstated", "n": r["n"]}
        for r in category_seniority_rows
    ]

    # The stats page's own panels. Three group-bys the homepage never
    # renders, over columns nothing else surfaces.
    #
    # workplace_type: remote/hybrid/onsite. Roughly half of listings say
    # nothing, and that half is reported as its own row rather than
    # hidden, because "most employers do not say" is the finding.
    workplace = conn.execute(
        f"""
        SELECT COALESCE(workplace_type, 'unstated') AS workplace, COUNT(*) AS n
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
        GROUP BY workplace_type
        ORDER BY n DESC
        """
    ).fetchall()

    # skills is comma-joined text, up to five terms per listing, on about
    # a third of rows. Split in Python: SQLite has no split, and pulling
    # 111k short strings is well under a second.
    skill_counts: dict = {}
    skilled = 0
    for (raw,) in conn.execute(
        f"""
        SELECT skills FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
          AND skills IS NOT NULL AND skills != ''
        """
    ):
        skilled += 1
        for term in raw.split(","):
            term = term.strip().lower()
            if term:
                skill_counts[term] = skill_counts.get(term, 0) + 1
    top_skills = [
        {"skill": k, "n": v}
        for k, v in sorted(skill_counts.items(), key=lambda kv: -kv[1])[:30]
    ]

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
        "category_seniority": category_seniority,
        "workplace": [dict(r) for r in workplace],
        "top_skills": top_skills,
        "skills_coverage": {"with_skills": skilled, "open_jobs": open_jobs_fresh},
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
