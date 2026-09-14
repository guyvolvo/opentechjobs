"""The Statistics sidebar, answered for the filters that are actually on.

Every number in that sidebar used to be global, which made it read as a
contradiction: the board said 98 open jobs in Israel and the panel next
to it said 2,623. So a filtered /api/stats now carries a "scoped" block
alongside the global one, and the frontend swaps field for field.

Two things decide whether this was worth doing, and both are checked
here rather than assumed.

The first is that scoped.open_jobs has to be the same number /api/jobs
reports as `total` for the same query string. Two figures disagreeing on
one screen is worse than either being missing, and they can only agree
by construction: both come from build_jobs_where with nothing bolted on.

The second is cost. compute_stats is twenty conn.execute calls, and
/api/facets already measured 0.30s from the precomputed artifact against
2.88s live off three. Scoping twenty queries would put seconds behind
every filter change, so the scoped block gets three passes over jobs and
no more. That is asserted, not documented: conn.execute is wrapped and
counted.

Run directly, no framework:  python tests/test_scoped_stats.py
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

os.environ.setdefault("ALERTS_TABLE", "test-alerts")
os.environ.setdefault("DATA_BUCKET", "test-bucket")
os.environ.setdefault("DATA_KEY", "jobs.db")
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import aggregates  # noqa: E402
import handler  # noqa: E402
import job_filters  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


NOW = datetime.now(timezone.utc)


def ago(days):
    """An ISO timestamp `days` in the past, or None for a NULL column."""
    if days is None:
        return None
    return (NOW - timedelta(days=days)).isoformat()


# id, company, country, posted days ago, first_seen days ago, closed
# days ago, confidence.
#
# Ages are spread far enough apart that a median over the Israeli subset
# (30) cannot be confused with the median over the whole board (20), and
# the oldest differ too (200 against 300). Numbers that happen to match
# would prove nothing about whether the filter reached the query.
JOBS = [
    ("a1", "wiz.io", "IL", 10, 0.5, None, "verified"),
    ("a2", "wiz.io", "IL", 30, 3, None, "verified"),
    ("a3", "wiz.io", "IL", 100, 40, None, "verified"),
    ("f1", "wiz.io", "IL", None, 0.3, None, "verified"),
    ("b1", "monday.com", "IL", 4, 4, None, "verified"),
    ("b2", "monday.com", "IL", 200, 200, None, "verified"),
    # Closed, so the board hides them and the throughput numbers must
    # not: closed_jobs_* is the reason the scoped block builds a second
    # WHERE at all.
    ("c1", "aidoc.com", "IL", 20, 20, 0.5, "verified"),
    ("c2", "aidoc.com", "IL", 50, 50, 3, "verified"),
    # Not verified, and the API defaults to verified only. If confidence
    # stopped being applied this row would show up in every count below.
    ("g1", "shady.io", "IL", 2, 0.1, None, "best_effort"),
    # Older than BOARD_MAX_AGE_DAYS, so FRESH_CLAUSE hides it.
    ("h1", "ancient.io", "IL", 400, 400, None, "verified"),
    ("d1", "acme.com", "US", 5, 5, None, "verified"),
    ("d2", "acme.com", "US", 6, 0.2, None, "verified"),
    ("e1", "globex.com", "US", 300, 300, None, "verified"),
    ("e2", "globex.com", "US", 8, 8, 2, "verified"),
]
# Twelve more American one-job companies, purely so the US subset has
# more companies than top_companies is allowed to return.
JOBS += [(f"z{i}", f"z{i}.example", "US", 15 + i, 15 + i, None, "verified")
         for i in range(12)]

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, company_domain TEXT, ats TEXT, title TEXT,
        location TEXT, department TEXT, seniority TEXT, workplace_type TEXT,
        url TEXT, posted_at TEXT, confidence TEXT, first_seen TEXT,
        last_seen TEXT, closed_at TEXT, skills TEXT, description TEXT,
        country TEXT, city TEXT, salary_text TEXT, salary_is_estimate INT
    )
""")
# compute_stats reads both of these for its global half. Empty is fine:
# the scoped block never touches either.
conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
conn.execute("""
    CREATE TABLE companies (
        domain TEXT PRIMARY KEY, ats TEXT, token TEXT, confidence TEXT,
        job_count INT, tried INT, error TEXT, first_seen TEXT, last_checked TEXT
    )
""")
for jid, domain, country, posted, seen, closed, confidence in JOBS:
    conn.execute(
        "INSERT INTO jobs (id, company_domain, ats, title, location, url, posted_at,"
        " confidence, first_seen, last_seen, closed_at, country)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, domain, "greenhouse", "Backend Engineer", "Tel Aviv, Israel",
         f"https://{domain}/{jid}", ago(posted), confidence, ago(seen), ago(0),
         ago(closed), country),
    )
conn.commit()
job_filters.register_functions(conn)
handler.get_connection = lambda: conn


class Counted:
    """A connection that remembers what was asked of it.

    __getattr__ passes everything else through, so anything the code
    under test does other than execute() behaves exactly as before.
    """

    def __init__(self, inner):
        self.inner = inner
        self.sql = []

    def execute(self, sql, *args, **kwargs):
        self.sql.append(sql)
        return self.inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


# Which requests get a scoped block at all.
#
# confidence is excluded because the board sends one on every single
# request: it defaults to "all" where the API defaults to "verified". If
# that counted as a filter, the plain page load would look filtered, the
# precomputed artifact would never be read again, and the scoped block
# would run on exactly the request it exists to stay out of.
plain = aggregates.compute_stats(conn, {})
check("an unfiltered request carries no scoped key",
      "scoped" not in plain, str(sorted(plain)))
check("and still answers everything it answered before",
      plain["totals"]["open_jobs"] == 21 and len(plain["open_jobs_history"]) == 14,
      str(plain["totals"]))

for variant in ("all", "verified", "best_effort"):
    body = aggregates.compute_stats(conn, {"confidence": variant})
    check(f"confidence={variant} on its own is still the unfiltered case",
          "scoped" not in body, str(sorted(body)))

check("a country filter is a real filter",
      aggregates.has_board_filters({"confidence": "all", "country": "IL"}) is True)

# An unusable value degrades the way it does everywhere else in
# job_filters: no clause, so the board is not narrowed, so a global
# answer is the honest one. ZZ is not in ALPHA2 and never reaches SQL.
check("an unusable country code leaves the request unfiltered",
      "scoped" not in aggregates.compute_stats(conn, {"country": "ZZ"}))
check("as does a malformed saved-job id",
      "scoped" not in aggregates.compute_stats(conn, {"ids": "not-an-id"}))

# The scoped numbers themselves, over Israel.
counted = Counted(conn)
il = aggregates.compute_scoped_stats(counted, {"country": "IL"})

# The budget, and the reason this file exists. Anything reading jobs is
# a pass over the table; the two schema probes (sqlite_master for the
# FTS index, PRAGMA table_info for the place columns) are not, but they
# are capped too so nobody re-probes per clause the way compute_facets
# does.
scans = [s for s in counted.sql if "FROM jobs" in s]
check("the scoped block is three passes over jobs, not twenty",
      len(scans) <= 3, f"{len(scans)} passes: {[' '.join(s.split())[:60] for s in scans]}")
# Everything else is a schema probe, apart from the one companies lookup
# that gives the top-companies chart its logos (aggregates._with_logos),
# which reads ten rows by primary key and is not a probe at all.
probes = [s for s in counted.sql if "FROM jobs" not in s and "FROM companies" not in s]
check("and the schema probes are not repeated per clause",
      len(probes) <= 2, f"{len(probes)} probes: {[' '.join(s.split())[:60] for s in probes]}")
check("and the logo lookup is one query, not one per company",
      sum("FROM companies" in s for s in counted.sql) <= 1, f"{len(counted.sql)} total executes")

check("open_jobs counts the filtered set", il["open_jobs"] == 6, str(il))
check("and differs from the global number",
      il["open_jobs"] != plain["totals"]["open_jobs"], str(il["open_jobs"]))

# The invariant that matters most. Both sides are build_jobs_where with
# nothing added, which is the only way these can be guaranteed equal for
# a filter nobody has thought of yet.
for filters in ({"country": "IL"}, {"country": "US"}, {"company": "wiz.io"},
                {"country": "IL", "confidence": "all"},
                {"country": "IL", "include_closed": "1"}):
    scoped = aggregates.compute_scoped_stats(conn, filters)
    total = handler.route_jobs(dict(filters))["total"]
    check(f"scoped open_jobs equals /api/jobs total for {filters}",
          scoped["open_jobs"] == total, f"{scoped['open_jobs']} vs {total}")

# Four jobs at wiz.io and two at monday.com, so a scoped block counting
# rows instead of companies would say 6 here.
check("companies_hiring counts companies, not rows",
      il["companies_hiring"] == 2, str(il["companies_hiring"]))
check("and the global one is larger",
      plain["totals"]["companies_hiring"] > il["companies_hiring"],
      str(plain["totals"]["companies_hiring"]))

# Ages: 4, 10, 30, 100 and 200 days. f1 has no posted_at and is left
# out rather than counted as new, same as the global age query does.
check("median_open_days is the filtered set's median",
      il["median_open_days"] == 30.0, str(il["median_open_days"]))
check("oldest_open_days is the filtered set's oldest",
      round(il["oldest_open_days"]) == 200, str(il["oldest_open_days"]))
check("both differ from the global answer",
      plain["age"]["median_open_days"] == 20.5 and round(plain["age"]["oldest_open_days"]) == 300,
      str(plain["age"]))
check("the 400-day listing is outside the board and outside the median",
      il["open_jobs"] == 6, str(il["open_jobs"]))

# Throughput has to see closed rows, which the board's own WHERE hides.
# Getting this wrong reports zero closings to every caller forever.
check("new_jobs_24h is scoped", il["new_jobs_24h"] == 2, str(il))
check("new_jobs_7d is scoped", il["new_jobs_7d"] == 4, str(il))
check("closed_jobs_24h survives the board hiding closed rows",
      il["closed_jobs_24h"] == 1, str(il))
check("closed_jobs_7d too", il["closed_jobs_7d"] == 2, str(il))
# The best_effort row was seen 0.1 days ago, so it is only absent from
# the two counts above because confidence reached the throughput query
# as well. Asking for it back is how we know that.
check("the best_effort listing is in none of them until it is asked for",
      aggregates.compute_scoped_stats(
          conn, {"country": "IL", "confidence": "all"})["new_jobs_24h"] == 3, str(il))

# top_companies keeps the global shape, cap included, so the frontend
# can render one list with one component.
check("top_companies is scoped to the filter",
      il["top_companies"] == [{"domain": "wiz.io", "n": 4}, {"domain": "monday.com", "n": 2}],
      str(il["top_companies"]))
us = aggregates.compute_scoped_stats(conn, {"country": "US"})
check("and capped at the same ten the global list uses",
      len(us["top_companies"]) == aggregates.SCOPED_TOP_COMPANIES
      and len(us["top_companies"]) == len(plain["top_companies"]),
      str(len(us["top_companies"])))
check("biggest first, same shape as the global list",
      sorted(us["top_companies"][0]) == sorted(plain["top_companies"][0])
      and us["top_companies"][0]["n"] == 2,
      str(us["top_companies"][:2]))

# Every field the contract names, present and named exactly as the
# global equivalents are, because the frontend swaps one for the other.
expected_keys = {"open_jobs", "companies_hiring", "new_jobs_24h", "new_jobs_7d",
                 "closed_jobs_24h", "closed_jobs_7d", "median_open_days",
                 "oldest_open_days", "top_companies"}
check("the scoped block carries exactly the contracted fields",
      set(il) == expected_keys, str(sorted(set(il) ^ expected_keys)))

# A filter matching nothing is a real answer, not a crash and not a
# global fallback.
empty = aggregates.compute_scoped_stats(conn, {"company": "nobody.example"})
check("an empty result set answers zeros and nulls",
      empty["open_jobs"] == 0 and empty["companies_hiring"] == 0
      and empty["median_open_days"] is None and empty["top_companies"] == [],
      str(empty))

# compute_stats attaches it, which is what the live path returns when
# the precomputed artifact is missing.
filtered = aggregates.compute_stats(conn, {"country": "IL"})
check("a filtered compute_stats carries the scoped block",
      filtered.get("scoped") == il, str(filtered.get("scoped")))
check("and leaves the global half global",
      filtered["totals"] == plain["totals"]
      and filtered["daily_new_jobs"] == plain["daily_new_jobs"],
      str(filtered["totals"]))

# route_stats: the artifact answers the global half, the scoped block is
# computed live on top of it. A filtered request must not be served a
# purely global body, and an unfiltered one must not pay for a scope it
# never asked for.
ARTIFACT = {"totals": {"open_jobs": 999}, "top_locations": [{"location": "Anywhere", "n": 9}],
            "top_locations_israel": [{"location": "Tel Aviv, Israel", "n": 4}],
            "freshness": {"last_checked": None, "minutes_since_update": None}, "meta": {}}
handler._precomputed = {}
handler._precomputed_json = lambda name: ARTIFACT

unscoped = handler.route_stats({"confidence": "all"})
check("route_stats still serves the artifact untouched when nothing is filtered",
      "scoped" not in unscoped and unscoped["totals"]["open_jobs"] == 999, str(sorted(unscoped)))
scoped_route = handler.route_stats({"confidence": "all", "country": "IL"})
check("a filtered request gets the scoped block",
      scoped_route["scoped"]["open_jobs"] == 7, str(scoped_route.get("scoped")))
check("the global half still comes from the artifact",
      scoped_route["totals"]["open_jobs"] == 999, str(scoped_route["totals"]))
check("freshness is still refreshed onto it",
      "freshness" in scoped_route and "meta" in scoped_route, str(sorted(scoped_route)))
check("israel_only still swaps its one field",
      handler.route_stats({"israel_only": "1"})["top_locations"] == ARTIFACT["top_locations_israel"])

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
