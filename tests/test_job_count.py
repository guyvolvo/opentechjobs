"""The total is optional, because it costs as much as the search itself.

/jobs used to answer every request with both the page and a COUNT(*) over
the same WHERE. For a typed search that WHERE is four substring scans over
every row, so the count doubled the price of a keystroke. A ten-character
word fired eight of those and the later ones sat in the queue until the
gateway gave up at 29 seconds, which a reader saw as HTTP 500.

The board now asks for the rows with count=skip and asks again with
count=only once they are on screen. This file is the contract for that:
the rows must not change, the number must not change, and a caller that
has never heard of the parameter must get exactly what it always got.

Run directly, no framework:  python tests/test_job_count.py
"""

import os
import sqlite3
import sys
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


conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, company_domain TEXT, ats TEXT, external_id TEXT, title TEXT,
        location TEXT, department TEXT, seniority TEXT, workplace_type TEXT,
        url TEXT, posted_at TEXT, confidence TEXT, first_seen TEXT,
        last_seen TEXT, closed_at TEXT, skills TEXT, description TEXT,
        salary_text TEXT, salary_is_estimate INT
    )
""")
# 70 engineers and 5 designers, so a search narrows and a page does not
# hold everything: enough to tell a real count from a page length.
conn.executemany(
    "INSERT INTO jobs (id, company_domain, ats, title, url, confidence, posted_at, first_seen, last_seen)"
    " VALUES (?, 'wiz.io', 'greenhouse', ?, 'https://example.com', 'verified',"
    " strftime('%Y-%m-%dT00:00:00+00:00', 'now'), datetime('now'), datetime('now'))",
    [(f"eng-{i:03d}", "Backend Engineer") for i in range(70)]
    + [(f"des-{i:03d}", "Product Designer") for i in range(5)])
conn.commit()
job_filters.register_functions(conn)
handler.get_connection = lambda: conn

both = handler.route_jobs({"limit": "50"})
skip = handler.route_jobs({"limit": "50", "count": "skip"})
only = handler.route_jobs({"limit": "50", "count": "only"})

check("the old shape is unchanged: a page of rows and the real total",
      both["total"] == 75 and len(both["jobs"]) == 50, repr((both["total"], len(both["jobs"]))))
check("count=skip returns the same page, in the same order, with no total",
      skip["total"] is None and [j["id"] for j in skip["jobs"]] == [j["id"] for j in both["jobs"]],
      repr(skip["total"]))
check("count=only returns the number and no rows at all",
      only["total"] == 75 and only["jobs"] == [] and only.get("count_only") is True,
      repr((only["total"], len(only["jobs"]))))

# The split is only worth anything if the two halves agree once a filter
# is on. A count that answered for a different WHERE would be worse than
# no count at all.
f = {"search": "engineer", "limit": "20"}
both_f = handler.route_jobs(dict(f))
skip_f = handler.route_jobs(dict(f, count="skip"))
only_f = handler.route_jobs(dict(f, count="only"))
check("with a search on, the deferred count equals the inline one",
      only_f["total"] == both_f["total"] == 70, repr((only_f["total"], both_f["total"])))
check("and the rows are still identical",
      [j["id"] for j in skip_f["jobs"]] == [j["id"] for j in both_f["jobs"]])

# Paging is the other reader of the total, and it pages on offset, which
# count=only must not quietly consume.
page2 = handler.route_jobs({"limit": "50", "offset": "50", "count": "skip"})
check("count=skip still pages: the second page is the remaining 25",
      len(page2["jobs"]) == 25 and page2["total"] is None, repr(len(page2["jobs"])))

# Saved alerts, the bootstrap writer and anything else built before this
# parameter existed send no count at all, and a typo must not silently
# cost someone their total.
check("no count parameter means both, as it always did", handler.route_jobs({"limit": "5"})["total"] == 75)
check("an unrecognised value means both rather than nothing",
      handler.route_jobs({"limit": "5", "count": "yes please"})["total"] == 75)
check("an empty value means both", handler.route_jobs({"limit": "5", "count": ""})["total"] == 75)
check("the value is read without regard to case or spacing",
      handler.route_jobs({"limit": "5", "count": "  SKIP "})["total"] is None)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
