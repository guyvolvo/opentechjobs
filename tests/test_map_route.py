"""/api/map: open listings per city and per country, for the map page.

Run directly, no framework:  python tests/test_map_route.py
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

import handler  # noqa: E402
from job_filters import register_functions  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


NOW = datetime.now(timezone.utc)
ago = lambda d: (NOW - timedelta(days=d)).isoformat(timespec="seconds")

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
register_functions(conn)
conn.execute("INSERT INTO companies (domain, ats, first_seen, last_checked) VALUES ('wix.com','greenhouse',?,?)", (ago(30), ago(0)))


def job(i, country, city, closed=None, confidence="verified", posted=1):
    conn.execute("INSERT INTO jobs (id, company_domain, ats, title, location, url, posted_at, first_seen, last_seen, closed_at, confidence, country, city)"
                 " VALUES (?, 'wix.com', 'greenhouse', 'Engineer', 'x', 'u', ?, ?, ?, ?, ?, ?, ?)",
                 (f"j{i:02d}", ago(posted), ago(posted), ago(0), closed, confidence, country, city))


job(1, "IL", "Tel Aviv"); job(2, "IL", "Tel Aviv"); job(3, "IL", "Haifa")
job(4, "IL", "Tel Aviv,Haifa")             # two cities: counts once for each
job(5, "US", "New York")
job(6, "IL,US", "Tel Aviv")                # two countries: nowhere to draw it
job(7, "IL", "")                           # country only: counts for the country, no city
job(8, "IL", "Tel Aviv", closed=ago(1))    # closed: out
job(9, "IL", "Tel Aviv", confidence="guess")  # unverified: out, as everywhere else on the board
job(10, "DE", "Berlin", posted=400)        # older than the freshness window: out
conn.commit()
handler.get_connection = lambda: conn

out = handler.route_map()
cities = {(cc, city): n for cc, city, n in out["cities"]}
countries = dict(out["countries"])
check("cities: each listed once per city it names, open and verified only",
      cities == {("IL", "Tel Aviv"): 3, ("IL", "Haifa"): 2, ("US", "New York"): 1}, repr(cities))
check("countries: every open listing with one country, city or not",
      countries == {"IL": 5, "US": 1}, repr(countries))
check("biggest first", [c[1] for c in out["cities"]][0] == "Tel Aviv" and out["countries"][0][0] == "IL")
check("labels name the countries drawn", out["labels"] == {"IL": "Israel", "US": "United States"}, repr(out["labels"]))

r = handler.lambda_handler({"requestContext": {"http": {"method": "GET"}}, "rawPath": "/api/map", "rawQueryString": ""}, None)
check("served at /api/map, cached at the edge for ten minutes",
      r["statusCode"] == 200 and "s-maxage=" in r["headers"].get("Cache-Control", "") and "max-age=600" in r["headers"]["Cache-Control"], repr(r["headers"]))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
