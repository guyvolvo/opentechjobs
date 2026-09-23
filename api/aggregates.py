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

from countries import label_for
from job_filters import (FRESH_CLAUSE, bool_param, build_jobs_where, category_sql,
                         has_fts_index, has_places, israel_clause)
from hot_companies import GOOGLE_FAVICON, HOT_COMPANIES, LOGO_PINS


def _with_logos(conn, rows: list[dict]) -> list[dict]:
    """Attach each company's resolved logo_url to a bar list's rows.

    The company chart drew its icons by guessing favicon paths on the
    domain, which misses every company whose icon lives anywhere else.
    Elbit's is under www.elbitsystems.com/themes/elbit/favicon/, so the
    biggest employer on the board showed a monogram in the chart while its
    own listing rows, which carry the resolved URL, showed the logo.
    Reported live.

    Looked up for the ten or so rows a list shows rather than joined into
    the grouping, which runs over every company.
    """
    domains = [r["domain"] for r in rows if r.get("domain")]
    if not domains:
        return rows
    try:
        found = {d: url for d, url in conn.execute(
            f"SELECT domain, logo_url FROM companies WHERE domain IN ({','.join('?' * len(domains))})",
            domains)}
    except Exception:
        # No companies table or no logo_url column yet (deploy skew). The
        # chart falls back to guessing, as it always did.
        return rows
    for r in rows:
        if r.get("domain"):
            r["logo_url"] = found.get(r["domain"])
    return rows


def top_companies_with_logos(conn, limit: int = 30) -> list[dict]:
    """The companies on the landing page's logo row, hand-picked ones first.

    HOT_COMPANIES (hot_companies.py) is big tech and well-known startups,
    picked by hand because nothing in the data says which companies those
    are. The picked companies with open jobs come first, busiest first. One
    without a resolved logo gets its LOGO_PINS image or Google's favicon for
    its domain. If fewer than `limit` qualify, the busiest companies not
    already shown fill the rest, and those need a resolved logo of their
    own: a row of monograms says nothing about who is hiring. Two domains
    that resolved to the same image show once.

    Counts every company's open jobs rather than a top slice, because a
    picked company can be far down the list. Once per precompute, over a
    few thousand groups.
    """
    ranked = [dict(r) for r in conn.execute(
        f"""
        SELECT company_domain AS domain, COUNT(*) AS n
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
        GROUP BY company_domain
        ORDER BY n DESC
        """
    )]
    hot = [r for r in ranked if r["domain"] in HOT_COMPANIES]
    rest = [r for r in ranked if r["domain"] not in HOT_COMPANIES][:limit * 6]
    candidates = _with_logos(conn, hot + rest)
    names: dict[str, str] = {}
    if candidates and any(c[1] == "company_name" for c in conn.execute("PRAGMA table_info(companies)")):
        domains = [r["domain"] for r in candidates]
        names = {d: n for d, n in conn.execute(
            f"SELECT domain, company_name FROM companies WHERE domain IN ({','.join('?' * len(domains))})",
            domains) if n}
    out, seen = [], set()
    for r in candidates:
        domain = r["domain"]
        url = LOGO_PINS.get(domain) or r.get("logo_url")
        if not url and domain in HOT_COMPANIES:
            url = GOOGLE_FAVICON.format(domain=domain)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"domain": domain, "name": names.get(domain), "n": r["n"], "logo_url": url})
        if len(out) == limit:
            break
    return out


# Each facet is counted with every OTHER filter applied, so this is the
# set each one sets aside before counting. Written down here rather than
# only inside counts_by/_place_scope so route_facets can ask "would this
# facet's scope be the whole board?" and answer from the precomputed
# artifact when it would.
FACET_SCOPE_DROPS = {
    "categories": ("department",),
    "locations": ("country", "city"),
    "companies": ("company",),
    "seniority": ("seniority",),
    "workplace": ("workplace",),
    "salary": ("salary_min", "salary_max", "salary_known"),
}

_FACETS_TTL_S = 300.0
_FACETS_MAX = 64
_facets_cache: dict[str, tuple[float, dict]] = {}


def _facets_key(params: dict) -> str:
    """Everything that changes the answer, in a stable order."""
    return "&".join(
        f"{k}={params[k]}" for k in sorted(params)
        if params.get(k) not in (None, "", False)
    )


def compute_facets(conn, params: dict, only: tuple[str, ...] | None = None) -> dict:
    """Cached in front of _compute_facets, which does the work.

    `only` narrows it to the facets the caller could not answer from the
    precomputed artifact. Counting a facet nobody will read is the
    single most expensive thing this endpoint used to do: the locations
    tree alone measured 10s of a 13s request on the box, and for a
    request filtered by country it was recomputing, row by row, the same
    whole-board answer already sitting in facets.json.

    Five minutes, because these are counts over a snapshot the applier
    rewrites every few minutes and a count that is one cycle old is not
    wrong in any way a reader can act on. The cache is per process and
    gunicorn runs two of them, so worst case is two cold computes per
    key rather than one; that is still two instead of one per request.
    """
    import time

    key = _facets_key(params) + "|" + ",".join(only or ())
    now = time.monotonic()
    hit = _facets_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    out = _compute_facets(conn, params, only)
    if len(_facets_cache) >= _FACETS_MAX:
        # Cheap and good enough: the board's filters are a small set and
        # this only ever fires when somebody has been exploring widely.
        _facets_cache.clear()
    _facets_cache[key] = (now + _FACETS_TTL_S, out)
    return out


def _compute_facets(conn, params: dict, only: tuple[str, ...] | None = None) -> dict:

    def counts_by(column_expr: str, exclude_param: str, limit: int) -> list[dict]:
        scoped = dict(params)
        scoped.pop(exclude_param, None)
        where_sql, args = build_jobs_where(scoped, has_fts_index(conn), has_places(conn))
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

    # Both place facets drop both place params, not just their own.
    #
    # counts_by's rule is that a facet is computed with every OTHER
    # filter applied, so ticking one of its own values never empties its
    # own list. Country and city are one control here, so the rule
    # applies to the pair. Scoping cities by the country already chosen
    # would be defensible, but scoping countries by the city already
    # chosen leaves exactly one country standing and no way back to the
    # rest.
    def _place_scope():
        scoped = dict(params)
        scoped.pop("country", None)
        scoped.pop("city", None)
        return build_jobs_where(scoped, has_fts_index(conn), has_places(conn))

    def country_counts(limit: int = 60) -> list[dict]:
        """One row per country, not per country LIST.

        country is comma-joined, so a plain GROUP BY would file
        "CA,IL,GB" as its own facet value and offer the reader a
        three-country checkbox that matches nothing else. The CTE splits
        it, which also means a job listing four offices in one country
        counts once rather than four times, since countries_of already
        deduplicated it before it was stored.
        """
        where_sql, args = _place_scope()
        rows = conn.execute(
            f"""
            WITH RECURSIVE split(code, rest) AS (
                SELECT '', country || ','
                FROM jobs
                WHERE {where_sql} AND country IS NOT NULL AND country != ''
                UNION ALL
                SELECT SUBSTR(rest, 1, INSTR(rest, ',') - 1),
                       SUBSTR(rest, INSTR(rest, ',') + 1)
                FROM split
                WHERE rest != ''
            )
            SELECT code AS value, COUNT(*) AS n
            FROM split
            WHERE code != ''
            GROUP BY code
            ORDER BY n DESC
            LIMIT ?
            """,
            [*args, limit],
        ).fetchall()
        return [{"value": r["value"], "label": label_for(r["value"]), "n": r["n"]}
                for r in rows]

    def city_counts_by_country() -> dict[str, list[dict]]:
        """City counts, counted per country rather than board-wide.

        A city on its own is not a facet a reader can use. There is a
        Cambridge in England and one in Massachusetts and the board
        carries both, so the pair is the answer. Both columns are split
        by the same kind of CTE the country facet uses, then joined back
        on the row they came from.

        That join is a cross product within one row, which is the
        definition being applied: a city belongs under a country when
        some job names both. A posting reading "Tel Aviv, Israel; New
        York, US" therefore files Tel Aviv under US too. That is wrong,
        and it is the price of a location column that never said which
        city went with which country. Jobs naming a single country,
        which is nearly all of them, come out exact.
        """
        where_sql, args = _place_scope()
        rows = conn.execute(
            f"""
            WITH RECURSIVE
            csplit(job, code, rest) AS (
                SELECT rowid, '', country || ','
                FROM jobs
                WHERE {where_sql} AND country IS NOT NULL AND country != ''
                  AND city IS NOT NULL AND city != ''
                UNION ALL
                SELECT job, SUBSTR(rest, 1, INSTR(rest, ',') - 1),
                       SUBSTR(rest, INSTR(rest, ',') + 1)
                FROM csplit
                WHERE rest != ''
            ),
            tsplit(job, name, rest) AS (
                SELECT rowid, '', city || ','
                FROM jobs
                WHERE {where_sql} AND country IS NOT NULL AND country != ''
                  AND city IS NOT NULL AND city != ''
                UNION ALL
                SELECT job, SUBSTR(rest, 1, INSTR(rest, ',') - 1),
                       SUBSTR(rest, INSTR(rest, ',') + 1)
                FROM tsplit
                WHERE rest != ''
            )
            SELECT csplit.code AS code, tsplit.name AS name, COUNT(*) AS n
            FROM csplit
            JOIN tsplit ON tsplit.job = csplit.job
            WHERE csplit.code != '' AND tsplit.name != ''
            GROUP BY csplit.code, tsplit.name
            ORDER BY n DESC, tsplit.name
            """,
            # The scope clause is written twice, so its arguments go in
            # twice as well.
            [*args, *args],
        ).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["code"], []).append({"value": r["name"], "n": r["n"]})
        return out

    def location_tree(country_limit: int = 40, city_limit: int = 25) -> list[dict]:
        """One entry per country, its cities nested underneath.

        Empty while the snapshot in hand predates the columns (see
        has_places). An empty filter list is a dropdown with nothing in
        it for one merge cycle; querying the columns anyway would 500
        every /api/facets call for that same cycle, which takes the
        category and company filters down with it.

        This replaces a flat facet over the raw location column, which
        offered "Tel Aviv", "Tel Aviv-Yafo, Tel Aviv, ISR" and
        "tel-aviv" as three separate choices for one place, and named no
        country anywhere. Both levels are deduplicated per job before
        they are stored, so a company listing four Tel Aviv offices on
        one posting counts once.

        The SQL orders both levels by n descending, so the slice keeps
        the 25 biggest cities rather than an arbitrary 25.
        """
        if not has_places(conn):
            return []
        cities = city_counts_by_country()
        return [
            {**c, "cities": cities.get(c["value"], [])[:city_limit]}
            for c in country_counts(country_limit)
        ]

    def salary_bounds() -> dict:
        """The shekel range the current result set actually occupies.

        The board's salary track needs ends, and a fixed pair written
        into the frontend would be a guess that goes stale the first
        time the estimates move. These are measured, with the salary
        filter itself dropped so dragging a handle cannot walk the track
        out from under the hand holding it, the same rule every other
        facet here follows.

        `known` is the count with a figure at all, which is what lets
        the rail say how much of the board the track can speak for
        rather than implying it speaks for all of it.
        """
        scoped = dict(params)
        for k in ("salary_min", "salary_max", "salary_known"):
            scoped.pop(k, None)
        where_sql, args = build_jobs_where(scoped, has_fts_index(conn), has_places(conn))
        row = conn.execute(
            f"""
            SELECT MIN(salary_min_ils), MAX(salary_max_ils), COUNT(salary_min_ils)
            FROM jobs WHERE {where_sql}
            """,
            args,
        ).fetchone()
        low, high, known = row[0], row[1], row[2]
        if low is None or high is None or not known:
            # No shekel figures in this result set at all. An empty dict
            # rather than a zero-width track, so the board can leave the
            # control out instead of drawing one that cannot move.
            return {}
        # The midpoint of each listing's own range, then the middle one.
        # SQLite has no median, and the sort runs only over rows that
        # carry a figure, which is a tenth of the board at most, so this
        # is a far smaller scan than the age median beside it in
        # compute_scoped_stats. A mean would be pulled around by the
        # handful of executive ranges; a median of ranges is the number
        # somebody reading "what does this pay" is actually asking for.
        if known > MEDIAN_ROW_BUDGET:
            return {"min": int(low), "max": int(high), "known": int(known)}
        mids = [
            r[0] for r in conn.execute(
                f"""
                SELECT (salary_min_ils + salary_max_ils) / 2.0 AS mid
                FROM jobs WHERE {where_sql} AND salary_min_ils IS NOT NULL
                ORDER BY mid
                """,
                args,
            ).fetchall()
        ]
        half = len(mids) // 2
        median = mids[half] if len(mids) % 2 else (mids[half - 1] + mids[half]) / 2
        return {"min": int(low), "max": int(high), "known": int(known),
                "median": int(round(median))}

    want = (lambda k: only is None or k in only)
    out = {}
    if want("categories"):
        out["categories"] = counts_by(category_sql(conn), "department", 20)
    if want("locations"):
        out["locations"] = location_tree()
    if want("companies"):
        out["companies"] = counts_by("company_domain", "company", 500)
    # Both are closed enums (probe.py's Job.seniority and
    # Job.workplace_type), so the board has always been able to draw the
    # options without asking. It could not draw the counts, and an
    # option list with no counts beside it is the one thing in the
    # filter rail that cannot tell a reader whether it is worth
    # clicking.
    if want("seniority"):
        out["seniority"] = counts_by("seniority", "seniority", 20)
    if want("workplace"):
        out["workplace"] = counts_by("workplace_type", "workplace", 10)
    if want("salary") and _has_salary_columns(conn):
        bounds = salary_bounds()
        if bounds:
            out["salary"] = bounds
    return out


# Rows with a figure, past which the median is not worth its sort. The
# Israeli board carries 1,266 and every filter a reader is likely to set
# stays far under this; the whole board is 97,085, which is the case
# this exists to refuse.
MEDIAN_ROW_BUDGET = 20_000


def _has_salary_columns(conn) -> bool:
    """Whether this snapshot carries the derived shekel columns.

    A snapshot written before loader/salary_range.py existed does not,
    and the facets route runs against whatever file is in hand. Asked
    here rather than read from SnapshotCaps because this module already
    holds a connection and the answer is one pragma; the query layer's
    own copy (caps.salary_ils) is what decides whether the filter runs.
    """
    try:
        return bool(conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('jobs') WHERE name = 'salary_min_ils'"
        ).fetchone()[0])
    except Exception:
        return False


COMPANY_SEARCH_LIMIT = 50


def search_companies(conn, params: dict) -> dict:
    """Companies whose domain or name contains `name`, with open-listing
    counts under every other active filter.

    The Companies dropdown lists the 500 biggest employers and its search
    box only filtered those, so any company past that line could not be
    found at all: typing "micr" said No matches while Microsoft had 18
    Israeli listings. Reported live. This asks the snapshot instead.

    Counted the way the facet is, with the company filter itself dropped,
    so a company already ticked does not narrow its own search. Two
    characters at least: one matches half the board and tells nobody
    anything.
    """
    name = str(params.get("name") or "").strip().lower()[:60]
    if len(name) < 2:
        return {"companies": []}
    scoped = dict(params)
    scoped.pop("company", None)
    scoped.pop("name", None)
    where_sql, args = build_jobs_where(scoped, has_fts_index(conn), has_places(conn))
    needle = "%" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        has_name = any(r[1] == "company_name" for r in conn.execute("PRAGMA table_info(companies)"))
    except Exception:
        has_name = False
    name_clause = (" OR company_domain IN (SELECT domain FROM companies"
                   " WHERE LOWER(company_name) LIKE ? ESCAPE '\\')") if has_name else ""
    rows = conn.execute(
        f"""
        SELECT company_domain AS value, COUNT(*) AS n
        FROM jobs
        WHERE {where_sql}
          AND (LOWER(company_domain) LIKE ? ESCAPE '\\'{name_clause})
        GROUP BY company_domain
        ORDER BY n DESC, company_domain
        LIMIT ?
        """,
        [*args, needle, *([needle] if has_name else []), COMPANY_SEARCH_LIMIT],
    ).fetchall()
    return {"companies": [dict(r) for r in rows]}


def has_board_filters(params: dict) -> bool:
    """Whether this request narrows the board at all.

    Decided from the WHERE the params produce, not from a list of
    parameter names, so a filter added to build_jobs_where later cannot
    quietly read as unfiltered here and get handed a global answer.
    handler.py's _unfiltered_confidence asks the same question for
    /api/facets and now calls this rather than keeping a second copy of
    the comparison that could drift from it.

    confidence is held constant on both sides, and that is the whole
    subtlety. The board sends a confidence on every single request: it
    defaults to "all" where the API defaults to "verified". Counting it
    as a filter would make every request read as filtered, so the
    precomputed artifact would never be used again and the scoped block
    would run on the plain page load it exists to stay out of. A request
    carrying nothing but confidence is therefore the unfiltered case.

    The FTS flag is constant on both sides too, for the same reason it
    is in _unfiltered_confidence: it only changes the keywords branch,
    and a request with keywords is filtered either way.

    A value build_jobs_where cannot use (a country code outside ALPHA2,
    a malformed job id) adds no clause, so it reads as unfiltered here
    as well. That is the same degrading every other caller of that
    function gets, and it is the right answer: the board itself was not
    narrowed either, so a global number is what matches what the reader
    is looking at.
    """
    # roles is held constant as well, and for the same reason: the board
    # sends roles=tech on every plain page load since 2026-09-21. The
    # precomputed artifacts carry a tech variant (loader/precompute.py),
    # so a request narrowed by nothing but roles has a ready answer.
    probe = {**params, "confidence": "verified"}
    probe.pop("roles", None)
    return build_jobs_where(probe, True) != build_jobs_where({"confidence": "verified"}, True)


# Matches the global top_companies' own LIMIT 10, so the frontend can
# swap one list for the other without re-cutting it.
SCOPED_TOP_COMPANIES = 10


def compute_scoped_stats(conn, params: dict) -> dict:
    """The few stats numbers that are properties of a result set rather
    than of the market, counted over the caller's own filters.

    Three queries, and that budget is the design rather than an
    accident. compute_stats runs twenty, most of them in the 14-day
    series and the day-by-day open-jobs reconstruction, and /api/facets
    already measures 0.30s served from the precomputed artifact against
    2.88s computed live off three. Scoping all twenty would put a
    multi-second request behind every filter change, so everything that
    only describes the whole market stays global and what is left is
    read in one grouped pass, one age pass and one throughput pass.
    Conditional SUM inside a pass, never a query per number.

    The scope is build_jobs_where's, unmodified, which is what makes
    open_jobs the same number /api/jobs reports as `total` for the same
    query string. Two figures on one screen disagreeing is worse than
    either being missing.
    """
    # Probed once and reused by both build_jobs_where calls below. Each
    # probe is its own read (sqlite_master, then PRAGMA table_info), and
    # calling them per clause the way compute_facets does would triple
    # that for no new information.
    fts, places = has_fts_index(conn), has_places(conn)
    where_sql, args = build_jobs_where(params, fts, places)

    # One grouped pass answers three fields. Summing the groups gives
    # the row count, counting the groups gives the companies, and the
    # first ten rows are the list. A separate COUNT and COUNT(DISTINCT)
    # would be two more scans for numbers already sitting here.
    per_company = conn.execute(
        f"""
        SELECT company_domain AS domain, COUNT(*) AS n
        FROM jobs
        WHERE {where_sql}
        GROUP BY company_domain
        ORDER BY n DESC
        """,
        args,
    ).fetchall()
    open_jobs = sum(r["n"] for r in per_company)
    # A NULL domain is a group here but not a company, and the global
    # COUNT(DISTINCT company_domain) does not count it either.
    companies_hiring = sum(1 for r in per_company if r["domain"] is not None)
    top_companies = _with_logos(conn, [{"domain": r["domain"], "n": r["n"]}
                                       for r in per_company[:SCOPED_TOP_COMPANIES]])

    # A row per job, same as the global age block: SQLite has no median,
    # and the sort runs over the filtered set, which is smaller than the
    # board by definition. A NULL posted_at is left out rather than read
    # as age zero, matching that query.
    ages = sorted(
        r["d"] for r in conn.execute(
            f"""
            SELECT julianday('now') - julianday(posted_at) AS d
            FROM jobs
            WHERE {where_sql} AND posted_at IS NOT NULL
            """,
            args,
        ).fetchall()
    )
    n = len(ages)
    median_days = None
    if n:
        mid = n // 2
        median_days = ages[mid] if n % 2 else (ages[mid - 1] + ages[mid]) / 2

    # Throughput is the one part that cannot run on the board's own
    # WHERE. It counts closings, the board hides closed rows by default,
    # so closed_jobs_* under the unmodified scope would be zero for
    # every caller. These two params lift exactly the two clauses the
    # global throughput query leaves out (it reads confidence and
    # nothing else), and every filter the caller did set still applies.
    flow_sql, flow_args = build_jobs_where(
        {**params, "include_closed": "1", "include_outdated": "1"}, fts, places)
    flow = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN julianday('now') - julianday(first_seen) <= 1 THEN 1 ELSE 0 END) AS added_24h,
          SUM(CASE WHEN julianday('now') - julianday(first_seen) <= 7 THEN 1 ELSE 0 END) AS added_7d,
          SUM(CASE WHEN closed_at IS NOT NULL
                    AND julianday('now') - julianday(closed_at) <= 1 THEN 1 ELSE 0 END) AS closed_24h,
          SUM(CASE WHEN closed_at IS NOT NULL
                    AND julianday('now') - julianday(closed_at) <= 7 THEN 1 ELSE 0 END) AS closed_7d
        FROM jobs
        WHERE {flow_sql}
        """,
        flow_args,
    ).fetchone()

    return {
        "open_jobs": open_jobs,
        "companies_hiring": companies_hiring,
        "new_jobs_24h": flow["added_24h"] or 0,
        "new_jobs_7d": flow["added_7d"] or 0,
        "closed_jobs_24h": flow["closed_24h"] or 0,
        "closed_jobs_7d": flow["closed_7d"] or 0,
        "median_open_days": round(median_days, 1) if median_days is not None else None,
        "oldest_open_days": round(ages[-1], 1) if n else None,
        "top_companies": top_companies,
    }


def compute_stats(conn, params: dict | None = None) -> dict:
    """Everything the homepage dashboard needs, as a handful of cheap SQL
    aggregates. All "since" comparisons use julianday() diffs rather than
    string comparison, since ISO8601-with-offset and datetime('now')'s
    format don't sort reliably against each other at day boundaries.

    params reads israel_only, which scopes top_locations to IL-tagged
    postings for the frontend's Location filter. Every other field here
    stays global and independent of the job board's own local filters.

    The exception is the "scoped" key, added only when the request
    carries a real filter. It answers "what does the market look like
    for the filters I have on right now" for the handful of numbers
    where that question means something, and it costs three queries
    against this function's twenty. See compute_scoped_stats for what
    is in it and why the rest is not.
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
    category = category_sql(conn)
    category_seniority_rows = conn.execute(
        f"""
        SELECT {category} AS category, seniority, COUNT(*) AS n
        FROM jobs
        WHERE closed_at IS NULL AND confidence = 'verified' AND {FRESH_CLAUSE}
          AND {category} IS NOT NULL
        GROUP BY {category}, seniority
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
    # strings, not a geocoded facet. The Israel match comes from
    # israel_clause rather than being spelled out again here, so this and
    # route_jobs' israel_only stay one heuristic instead of two that drift.
    il_clause, il_args = israel_clause(has_places(conn))

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

    payload = {
        "meta": meta,
        "open_jobs_by_ats": [dict(r) for r in by_ats],
        "category_seniority": category_seniority,
        "workplace": [dict(r) for r in workplace],
        "top_skills": top_skills,
        "skills_coverage": {"with_skills": skilled, "open_jobs": open_jobs_fresh},
        "top_companies": _with_logos(conn, [dict(r) for r in top_companies]),
        "top_departments": [dict(r) for r in top_departments],
        "top_locations": [dict(r) for r in top_locations],
        "top_movers_7d": _with_logos(conn, [dict(r) for r in top_movers]),
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
    # Absent, not empty, when nothing is filtered. The key existing is
    # how the frontend knows there is a result set worth describing, and
    # an unfiltered request must keep answering exactly what it answered
    # before this shipped, precomputed artifact included.
    if has_board_filters(params):
        payload["scoped"] = compute_scoped_stats(conn, params)
    return payload
