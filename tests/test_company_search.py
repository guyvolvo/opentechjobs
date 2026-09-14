"""Searching for a company from the Companies dropdown.

The dropdown lists the 500 biggest employers, and its search box used to
filter only those, so "micr" said No matches while Microsoft had listings.
/api/companies/search asks the snapshot for any company whose domain or
name contains the text, counted under the board's other filters.

Run directly, no framework:  python tests/test_company_search.py
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

import aggregates  # noqa: E402
import handler  # noqa: E402
import job_filters  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def build(with_names=True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE jobs (id TEXT PRIMARY KEY, company_domain TEXT, ats TEXT, title TEXT,
        location TEXT, department TEXT, seniority TEXT, workplace_type TEXT, url TEXT, posted_at TEXT,
        confidence TEXT, first_seen TEXT, last_seen TEXT, closed_at TEXT, skills TEXT, description TEXT)""")
    cols = "domain TEXT PRIMARY KEY, ats TEXT" + (", company_name TEXT" if with_names else "")
    conn.execute(f"CREATE TABLE companies ({cols})")
    companies = [("microsoft.com", "microsoft", "Microsoft"), ("tripactions.com", "greenhouse", "Navan"),
                 ("micro_focus.com", "workday", "Micro Focus"), ("wix.com", "greenhouse", "Wix")]
    for d, ats, name in companies:
        if with_names:
            conn.execute("INSERT INTO companies VALUES (?,?,?)", (d, ats, name))
        else:
            conn.execute("INSERT INTO companies VALUES (?,?)", (d, ats))
    jobs = [("m1", "microsoft.com", "Tel Aviv, Israel"), ("m2", "microsoft.com", "Redmond, US"),
            ("n1", "tripactions.com", "Tel Aviv, Israel"), ("f1", "micro_focus.com", "London, UK"),
            ("w1", "wix.com", "Tel Aviv, Israel"), ("w2", "wix.com", "Tel Aviv, Israel")]
    for jid, d, loc in jobs:
        conn.execute("INSERT INTO jobs (id, company_domain, ats, title, location, confidence, posted_at, first_seen, last_seen)"
                     " VALUES (?,?,'x','Engineer',?,'verified',datetime('now'),datetime('now'),datetime('now'))", (jid, d, loc))
    conn.commit()
    job_filters.register_functions(conn)
    return conn


conn = build()
found = aggregates.search_companies(conn, {"name": "micr"})["companies"]
check("a domain search finds companies past any top-N cut",
      [r["value"] for r in found] == ["microsoft.com", "micro_focus.com"], repr(found))
check("counts are open listings", found[0]["n"] == 2, repr(found))
check("the company's own name is searched too",
      [r["value"] for r in aggregates.search_companies(conn, {"name": "navan"})["companies"]] == ["tripactions.com"])
check("case does not matter", aggregates.search_companies(conn, {"name": "MICROSOFT"})["companies"][0]["value"] == "microsoft.com")
check("an underscore is a literal character, not a wildcard",
      [r["value"] for r in aggregates.search_companies(conn, {"name": "o_f"})["companies"]] == ["micro_focus.com"],
      repr(aggregates.search_companies(conn, {"name": "o_f"})["companies"]))
check("one character asks nothing", aggregates.search_companies(conn, {"name": "m"})["companies"] == [])
check("other filters still count", aggregates.search_companies(conn, {"name": "micro", "search": "nothing-matches-this"})["companies"] == [])
check("the company filter itself is ignored, so a ticked company does not hide the others",
      len(aggregates.search_companies(conn, {"name": "micr", "company": "wix.com"})["companies"]) == 2)

bare = build(with_names=False)
check("a snapshot without company names still searches domains",
      [r["value"] for r in aggregates.search_companies(bare, {"name": "micr"})["companies"]] == ["microsoft.com", "micro_focus.com"])

handler.get_connection = lambda: conn
resp = handler.lambda_handler({"rawPath": "/api/companies/search", "rawQueryString": "name=wix",
                               "queryStringParameters": {"name": "wix"},
                               "requestContext": {"http": {"method": "GET"}}}, None)
import json  # noqa: E402
body = json.loads(resp["body"])
check("the route answers before /companies", resp["statusCode"] == 200 and body["companies"][0]["value"] == "wix.com", repr(resp)[:200])

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
