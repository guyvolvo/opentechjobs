"""Listings that share a posting time still page cleanly.

Workday and Amazon often post a date with no time, so hundreds of rows can
share one posted_at to the second. /jobs sorted on that alone, and SQLite
may order tied rows differently from one query to the next, so paging with
LIMIT/OFFSET could show a listing on two pages and another on none. Both
the API and the bootstrap first page now end the order on id.

Run directly, no framework:  python tests/test_jobs_order_tiebreak.py
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


def build(path=":memory:"):
    conn = sqlite3.connect(path)
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
    # 120 listings on one timestamp, inserted in a scrambled id order so
    # the table's own storage order is no help, plus two that sort ahead.
    ids = [f"job-{(i * 37) % 120:03d}" for i in range(120)]
    conn.executemany(
        "INSERT INTO jobs (id, company_domain, ats, title, url, confidence, posted_at, first_seen, last_seen)"
        " VALUES (?, 'amazon.com', 'amazon', 'Engineer', 'https://example.com', 'verified',"
        " strftime('%Y-%m-%dT00:00:00+00:00', 'now'), datetime('now'), datetime('now'))",
        [(i,) for i in ids])
    conn.executemany(
        "INSERT INTO jobs (id, company_domain, ats, title, url, confidence, posted_at, first_seen, last_seen)"
        " VALUES (?, 'stripe.com', 'greenhouse', 'Engineer', 'https://example.com', 'verified',"
        " strftime('%Y-%m-%dT06:00:00+00:00', 'now'), datetime('now'), datetime('now'))",
        [("aaa-newest",), ("zzz-newest",)])
    conn.commit()
    job_filters.register_functions(conn)
    return conn


conn = build()
handler.get_connection = lambda: conn
pages = [handler.route_jobs({"limit": "50", "offset": str(off)})["jobs"] for off in (0, 50, 100)]
seen = [j["id"] for page in pages for j in page]

check("every listing appears once across the pages", len(seen) == 122 and len(set(seen)) == 122,
      f"{len(seen)} rows, {len(set(seen))} distinct")
check("the newer posting time still comes first", seen[:2] == ["zzz-newest", "aaa-newest"], repr(seen[:2]))
tied = seen[2:]
check("tied listings come out in id order, newest id first", tied == sorted(tied, reverse=True), repr(tied[:6]))
again = [j["id"] for j in handler.route_jobs({"limit": "50", "offset": "50"})["jobs"]]
check("asking for the same page twice gives the same rows", again == [j["id"] for j in pages[1]])

with tempfile.TemporaryDirectory() as tmp:
    db_path = Path(tmp) / "jobs.db"
    file_conn = build(str(db_path))
    file_conn.close()
    boot = bootstrap.build(db_path)
    boot_rows = boot.get("jobs", {}).get("jobs", []) if isinstance(boot.get("jobs"), dict) else boot.get("jobs", [])
    boot_ids = [j["id"] for j in boot_rows]
    handler.get_connection = lambda: conn
    api_ids = [j["id"] for j in handler.route_jobs({"limit": str(len(boot_ids) or 50), "offset": "0"})["jobs"]]
    check("the bootstrap first page is the API's first page, row for row",
          boot_ids and boot_ids == api_ids, f"boot {boot_ids[:4]} api {api_ids[:4]}")

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
