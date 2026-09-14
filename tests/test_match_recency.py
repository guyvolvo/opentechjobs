"""Best matches weighs recency alongside skill overlap.

Ranked on overlap alone, the top of a live Israeli DevOps match was
postings 29 to 42 days old, with a three-day-old role that shared one
skill fewer below all of them. Every MATCH_RECENCY_DAYS since posting now
costs one matched skill. Dates here are relative to today, so the test
means the same thing whenever it runs.

Run directly, no framework:  python tests/test_match_recency.py
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
import job_filters  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


STEP = handler.MATCH_RECENCY_DAYS
MINE = "Python,Azure,Linux,CI/CD,Git,Ansible,AWS"

# (id, skills, days since posting)
JOBS = [
    ("stale5", "Python,Linux,Git,Ansible,AWS", 3 * STEP),      # 5 skills, 3 steps old -> 2
    ("fresh4", "Python,Linux,Git,AWS", 3),                     # 4 skills, fresh -> 4
    ("fresh1", "Python", 1),                                   # 1 skill, fresh -> 1
    ("mid5", "Python,Azure,Linux,Git,AWS", STEP + 1),          # 5 skills, 1 step old -> 4
    ("none", "JavaScript,React", 0),                           # 0 skills, newest -> 0
    ("fresh4b", "Azure,CI/CD,Git,AWS", 5),                     # 4 skills, fresh, older than fresh4 -> 4
]

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, company_domain TEXT, ats TEXT, title TEXT,
        location TEXT, department TEXT, seniority TEXT, workplace_type TEXT,
        url TEXT, posted_at TEXT, confidence TEXT, first_seen TEXT,
        last_seen TEXT, closed_at TEXT, skills TEXT, description TEXT,
        salary_text TEXT, salary_is_estimate INT
    )
""")
for jid, skills, age in JOBS:
    conn.execute(
        "INSERT INTO jobs (id, company_domain, ats, title, url, posted_at, confidence,"
        " first_seen, last_seen, skills) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (jid, "x.com", "greenhouse", jid, "http://x", days_ago(age), "verified",
         days_ago(age), days_ago(0), skills),
    )
conn.commit()
job_filters.register_functions(conn)
handler.get_connection = lambda: conn

res = handler.route_jobs({"skills": MINE, "sort": "match"})
ids = [j["id"] for j in res["jobs"]]

check("a fresh 4-skill match outranks a 5-skill match three steps old",
      ids.index("fresh4") < ids.index("stale5"), repr(ids))
check("a 5-skill match one step old ties a fresh 4-skill one and the newer wins",
      ids.index("fresh4") < ids.index("mid5"), repr(ids))
check("among equal adjusted scores the order is newest first",
      ids[:3] == ["fresh4", "fresh4b", "mid5"], repr(ids))
check("a fresh 1-skill match does not jump a strong older one",
      ids.index("stale5") < ids.index("fresh1"), repr(ids))
check("a listing with no matching skill is not on the list, however new",
      "none" not in ids and res["total"] == len(JOBS) - 1, repr(ids))
check("match_score stays the plain skill count",
      {j["id"]: j["match_score"] for j in res["jobs"]}["stale5"] == 5,
      repr({j["id"]: j["match_score"] for j in res["jobs"]}))

# Another sort is still exactly that sort.
by_age = [j["id"] for j in handler.route_jobs({"skills": MINE, "sort": "age"})["jobs"]]
check("sort=age is plain newest first", by_age[:2] == ["fresh1", "fresh4"], repr(by_age))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
