"""The precomputed country page says what it contains, and contains it.

This file stands in for a live API answer, so the only thing that makes
it safe is that the frontend can tell whether it is the right answer. Two
ways that goes wrong: the params string drifts from what app.js asks for,
so a correct page is silently ignored, or the rows drift from what the
API would return, so a wrong page is rendered as though it were real.
The second is the one that matters, and it is why the country filter is
built by build_jobs_where rather than written out here.

Run directly, no framework:  python tests/test_bootstrap_country.py
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "loader"))

os.environ.setdefault("ALERTS_TABLE", "test-alerts")
os.environ.setdefault("DATA_BUCKET", "test-bucket")
os.environ.setdefault("DATA_KEY", "jobs.db")
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import bootstrap  # noqa: E402
import handler  # noqa: E402
import job_filters  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def make_db(path, with_places=True):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    places = ", country TEXT, city TEXT" if with_places else ""
    conn.execute(f"""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, company_domain TEXT, ats TEXT, external_id TEXT, title TEXT,
            location TEXT, department TEXT, seniority TEXT, workplace_type TEXT,
            url TEXT, posted_at TEXT, confidence TEXT, first_seen TEXT,
            last_seen TEXT, closed_at TEXT, skills TEXT, description TEXT,
            salary_text TEXT, salary_is_estimate INT{places}
        )
    """)
    cols = "id, company_domain, ats, title, location, url, confidence, posted_at, first_seen, last_seen"
    vals = "?, ?, 'greenhouse', 'Engineer', ?, 'https://example.com', 'verified', ?, datetime('now'), datetime('now')"
    if with_places:
        cols += ", country"
        vals += ", ?"
    # Interleaved by posting time, so a page that ignored the filter
    # would come back with a different set AND a different order.
    rows = [
        ("il-1", "wix.com", "Tel Aviv, Israel", "2026-09-17T10:00:00+00:00", ",IL,"),
        ("us-1", "stripe.com", "New York, NY", "2026-09-17T09:00:00+00:00", ",US,"),
        ("il-2", "monday.com", "Haifa, Israel", "2026-09-17T08:00:00+00:00", ",IL,"),
        ("us-2", "figma.com", "San Francisco, CA", "2026-09-17T07:00:00+00:00", ",US,"),
        ("il-3", "cyberark.com", "Petah Tikva, Israel", "2026-09-17T06:00:00+00:00", ",IL,"),
    ]
    conn.executemany(
        f"INSERT INTO jobs ({cols}) VALUES ({vals})",
        [r if with_places else r[:4] for r in rows])
    conn.commit()
    job_filters.register_functions(conn)
    return conn


with tempfile.TemporaryDirectory() as tmp:
    db_path = Path(tmp) / "jobs.db"
    make_db(str(db_path)).close()

    boot = bootstrap.build(db_path, {"country": "IL"})
    ids = [j["id"] for j in boot["jobs"]["jobs"]]

    # The exact string app.js compares against, country first. Getting
    # this wrong costs nothing but the speed-up; getting the rows wrong
    # would be a lie, which is the check below it.
    check("the params string is the one app.js builds for this view",
          boot["params"] == "country=IL&confidence=all&roles=tech&sort=age&dir=asc&limit=50&offset=0",
          boot["params"])
    check("only the country's listings are in it", ids == ["il-1", "il-2", "il-3"], repr(ids))
    check("and the total counts only those", boot["jobs"]["total"] == 3, repr(boot["jobs"]["total"]))

    # The whole point: this page stands in for the API's answer, so it
    # has to be the API's answer.
    conn = make_db(":memory:")
    handler.get_connection = lambda: conn
    api = handler.route_jobs({"country": "IL", "confidence": "all", "sort": "age",
                              "dir": "asc", "limit": "50", "offset": "0"})
    check("row for row, it is what the API would have answered",
          [j["id"] for j in api["jobs"]] == ids, repr([j["id"] for j in api["jobs"]]))
    check("and reports the same total", api["total"] == boot["jobs"]["total"],
          f'api={api["total"]} boot={boot["jobs"]["total"]}')

    # The default view must not have picked up the country filter.
    plain = bootstrap.build(db_path)
    check("the default page is still every country",
          [j["id"] for j in plain["jobs"]["jobs"]] == ["il-1", "us-1", "il-2", "us-2", "il-3"],
          repr([j["id"] for j in plain["jobs"]["jobs"]]))
    check("and says so in its params",
          plain["params"] == "confidence=all&roles=tech&sort=age&dir=asc&limit=50&offset=0", plain["params"])

# A snapshot from before the country column exists. The API degrades to
# an unfiltered answer here; this file must refuse to publish instead,
# because a global page labelled country=IL renders as though it were
# the real thing.
with tempfile.TemporaryDirectory() as tmp:
    db_path = Path(tmp) / "old.db"
    make_db(str(db_path), with_places=False).close()
    try:
        bootstrap.build(db_path, {"country": "IL"})
        check("a snapshot with no country column publishes nothing", False, "it returned a payload")
    except ValueError as e:
        check("a snapshot with no country column publishes nothing", "country column" in str(e), str(e))
    ok = bootstrap.build(db_path)
    check("but the default page is still built from it", len(ok["jobs"]["jobs"]) == 5,
          repr(len(ok["jobs"]["jobs"])))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
