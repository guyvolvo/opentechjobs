"""Which boards earn the fast lane.

The first version of this only understood an alert that named a company.
Then the live table said every active alert filtered by keyword or by
country instead, so that rule covered nobody and the ScaleOps report that
prompted the whole thing would have happened again unchanged.

So a board also earns it by answering somebody's alert lately. An alert
for "DevOps in Israel" names no board, but the boards that answered it
last fortnight are the ones it will be answered by next.

Run directly, no framework:  python tests/test_watched_domains.py
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

import alerts  # noqa: E402
import job_filters  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


NOW = datetime.now(timezone.utc)
RECENT = (NOW - timedelta(days=3)).isoformat()
STALE = (NOW - timedelta(days=90)).isoformat()

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, company_domain TEXT, title TEXT, location TEXT,
        department TEXT, description TEXT, closed_at TEXT, confidence TEXT,
        posted_at TEXT, first_seen TEXT, last_seen TEXT, skills TEXT
    )
""")
rows = [
    # Answers "DevOps", seen recently. This is the ScaleOps case.
    ("a", "scaleops.com", "Senior DevOps Engineer", "Tel Aviv, Israel", RECENT),
    ("b", "wiz.io", "DevOps Lead", "Tel Aviv, Israel", RECENT),
    # Answers it, but three months ago. That board has gone quiet and
    # does not deserve to hold a fast lane forever.
    ("c", "oldco.com", "DevOps Engineer", "Tel Aviv, Israel", STALE),
    # Recent, but answers nothing anybody asked about.
    ("d", "grocer.com", "Shelf Stacker", "Haifa, Israel", RECENT),
]
for jid, domain, title, loc, seen in rows:
    conn.execute(
        "INSERT INTO jobs (id, company_domain, title, location, department, confidence,"
        " posted_at, first_seen, last_seen) VALUES (?,?,?,?,'R&D','verified',?,?,?)",
        (jid, domain, title, loc, seen, seen, seen))
conn.commit()
job_filters.register_functions(conn)

keyword_alert = [{"alert_id": "1", "filter": {"search": "devops"}}]
got = alerts._recently_matching_domains(conn, keyword_alert)
check("a keyword alert puts the boards that answered it in the fast lane",
      "scaleops.com" in got and "wiz.io" in got, repr(sorted(got)))
check("a board that answered it three months ago is not in it",
      "oldco.com" not in got, repr(sorted(got)))
check("nor is a board that answers nothing anybody asked for",
      "grocer.com" not in got, repr(sorted(got)))

# The original rule still works, and the two are a union rather than a
# choice: naming a company follows it even before it has posted once.
named = alerts._watched_domains([{"filter": {"company": "ScaleOps.com, Wiz.io"}}])
check("an alert that names companies still follows them",
      named == {"scaleops.com", "wiz.io"}, repr(named))
check("and a brand-new company with no postings yet is still followed",
      "newco.com" in alerts._watched_domains([{"filter": {"company": "newco.com"}}]))
check("an alert that names nobody contributes nothing to that half",
      alerts._watched_domains([{"filter": {"search": "devops"}}]) == set())

# Failures upstream must cost the old schedule, never an exception: this
# runs inside the five-minute maintenance pass that also sends digests.
check("an unreadable filter does not take the other alerts down",
      alerts._recently_matching_domains(
          conn, [{"alert_id": "bad", "filter": {"posted_within_days": "not a number"}},
                 {"alert_id": "2", "filter": {"search": "devops"}}]) >= {"scaleops.com"})
check("no alerts at all is an empty set, not an error",
      alerts._recently_matching_domains(conn, []) == set())

# A country-wide alert matches everything, and the cap is what stops the
# fast lane from quietly becoming the whole road.
check("the per-alert cap is a real number, not a suggestion",
      isinstance(alerts.WATCH_DOMAINS_PER_ALERT, int) and alerts.WATCH_DOMAINS_PER_ALERT <= 500,
      repr(alerts.WATCH_DOMAINS_PER_ALERT))
wide = alerts._recently_matching_domains(conn, [{"alert_id": "3", "filter": {}}])
check("an alert with no filter at all is bounded by that cap",
      len(wide) <= alerts.WATCH_DOMAINS_PER_ALERT)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
