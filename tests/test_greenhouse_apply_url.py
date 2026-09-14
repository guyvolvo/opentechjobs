"""A Greenhouse listing's Apply link is Greenhouse's own application page.

Greenhouse reports each job's absolute_url, which for most companies is
their own careers site with ?gh_jid= on the end. Whether that page can find
the job is up to the site, and Taboola's cannot: every job link redirected
to its generic jobs list. Reported live. The embedded application page
works for every board, so the API builds that link, and old rows get it too.

Run directly, no framework:  python tests/test_greenhouse_apply_url.py
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
import probe  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def build(with_companies=True):
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
    if with_companies:
        conn.execute("CREATE TABLE companies (domain TEXT PRIMARY KEY, ats TEXT, token TEXT, company_name TEXT, logo_url TEXT)")
        conn.executemany("INSERT INTO companies VALUES (?,?,?,?,?)", [
            ("taboola.com", "greenhouse", "taboola", "Taboola", None),
            ("navan.com", None, None, None, None),          # a demoted alias, no token
            ("wix.com", "lever", "wix", "Wix", None),
        ])
    rows = [
        ("t1", "taboola.com", "greenhouse", "8200807", "Site Reliability Engineer",
         "https://www.taboola.com/careers/job/8200807?gh_jid=8200807"),
        ("n1", "navan.com", "greenhouse", "6906952", "Account Executive", "https://navan.com/careers/openings?gh_jid=6906952"),
        ("w1", "wix.com", "lever", "abc", "Backend Engineer", "https://jobs.lever.co/wix/abc"),
    ]
    for jid, domain, ats, ext, title, url in rows:
        conn.execute("INSERT INTO jobs (id, company_domain, ats, external_id, title, url, confidence, posted_at, first_seen, last_seen)"
                     " VALUES (?,?,?,?,?,?, 'verified', datetime('now'), datetime('now'), datetime('now'))",
                     (jid, domain, ats, ext, title, url))
    conn.commit()
    job_filters.register_functions(conn)
    return conn


conn = build()
handler.get_connection = lambda: conn
urls = {j["id"]: j["url"] for j in handler.route_jobs({})["jobs"]}
check("a Greenhouse row links to Greenhouse's application page",
      urls["t1"] == "https://job-boards.greenhouse.io/embed/job_app?for=taboola&token=8200807", urls["t1"])
check("a company with no Greenhouse token keeps its stored link",
      urls["n1"] == "https://navan.com/careers/openings?gh_jid=6906952", urls["n1"])
check("other ATSes keep their stored link", urls["w1"] == "https://jobs.lever.co/wix/abc", urls["w1"])

handler._description_from_s3 = lambda job_id: None
detail = handler.route_job_detail("t1")
check("the job sheet's Apply link is the same one",
      detail and detail["url"] == urls["t1"], repr(detail and detail.get("url")))

bare = build(with_companies=False)
handler.get_connection = lambda: bare
handler._has_company_column.cache_clear() if hasattr(handler._has_company_column, "cache_clear") else None
urls = {j["id"]: j["url"] for j in handler.route_jobs({})["jobs"]}
check("a snapshot without the companies columns still answers, with the stored link",
      urls["t1"].startswith("https://www.taboola.com/"), repr(urls))

# New rows carry the working link from the start.
probe.get_json = lambda sess, url: {"jobs": [{"id": 8200807, "internal_job_id": 1, "title": "SRE",
                                              "location": {"name": "Tel Aviv, Israel"},
                                              "absolute_url": "https://www.taboola.com/careers/job/8200807?gh_jid=8200807",
                                              "updated_at": "2026-09-14T06:00:00Z", "content": "x"}]}
job = probe.f_greenhouse(None, "taboola")[0]
check("probe stores the application page too",
      job.url == "https://job-boards.greenhouse.io/embed/job_app?for=taboola&token=8200807", job.url)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
