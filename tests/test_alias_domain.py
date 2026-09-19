"""Two domains, one board: the real name wins over the guessed one.

discover_companies.py falls back to "{token}.com" when it cannot read a
domain off a board, so tenableinc.com, atbayjobs.com, tipaltisolutions.com
and wix2.com all own boards on the live site. When the real domain then
arrives (tenable.com, from a companies.yml pin), the old rule kept the
first resolved owner and demoted the real one as an alias. Measured
2026-09-19: 25 of 66 TechAviv pins hit exactly this.

Run directly, no framework:  python tests/test_alias_domain.py
"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

import load_to_sqlite as lts  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
ago = lambda d: (NOW - timedelta(days=d)).isoformat(timespec="seconds")


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    lts._migrate(conn)
    return conn


def resolved_company(conn, domain, ats, token, jobs):
    conn.execute("INSERT INTO companies (domain, ats, token, confidence, job_count, first_seen, last_checked)"
                 " VALUES (?, ?, ?, 'verified', ?, ?, ?)", (domain, ats, token, len(jobs), ago(40), ago(1)))
    for ext, title, first_seen, posted in jobs:
        jid = lts.job_id(domain, ats, ext, f"https://x/{ext}", title)
        conn.execute("INSERT INTO jobs (id, company_domain, ats, external_id, title, url, posted_at, confidence, first_seen, last_seen)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?)", (jid, domain, ats, ext, title, f"https://x/{ext}", posted, first_seen, ago(1)))


def load(conn, records):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "resolved.json"
        path.write_text(json.dumps(records), encoding="utf-8")
        lts.load_resolved(conn, path)


def company(conn, domain):
    return conn.execute("SELECT ats, token, error FROM companies WHERE domain = ?", (domain,)).fetchone()


def open_jobs(conn, domain):
    return conn.execute("SELECT external_id, first_seen, posted_at FROM jobs WHERE company_domain = ? AND closed_at IS NULL ORDER BY external_id",
                        (domain,)).fetchall()


def record(domain, ats, token, jobs, extra=None):
    return {"domain": domain, "ats": ats, "token": token, "job_count": len(jobs), "tried": 1, "error": None,
            "retryable": False, "jobs": [dict({"ats": ats, "external_id": ext, "title": t, "url": f"https://x/{ext}",
                                                 "location": "Tel Aviv, Israel", "posted_at": posted}, **(extra or {}))
                                           for ext, t, posted in jobs]}


# 1. The guessed domain owns the board; the real one arrives later and takes it over.
conn = fresh_db()
resolved_company(conn, "tenableinc.com", "greenhouse", "tenableinc",
                 [("101", "Backend Engineer", ago(30), ago(31)), ("102", "SRE", ago(12), ago(13))])
load(conn, [record("tenable.com", "greenhouse", "tenableinc",
                   [("101", "Backend Engineer", ago(31)), ("102", "SRE", ago(13)), ("103", "New Role", ago(0))])])
check("the real domain is the resolved one", company(conn, "tenable.com")["ats"] == "greenhouse")
guessed = company(conn, "tenableinc.com")
check("the guessed domain is demoted as its alias, whichever resolved first",
      guessed["ats"] is None and guessed["error"] == "alias of tenable.com (both resolve to greenhouse:tenableinc)", repr(dict(guessed)))
check("the guessed domain's jobs are closed", open_jobs(conn, "tenableinc.com") == [])
rows = open_jobs(conn, "tenable.com")
check("the postings carry over their real age instead of reading as found today",
      [r["external_id"] for r in rows] == ["101", "102", "103"]
      and rows[0]["first_seen"] == ago(30) and rows[1]["first_seen"] == ago(12)
      and rows[2]["first_seen"] > ago(1), repr([dict(r) for r in rows]))
check("greenhouse posted_at is the ATS's own, not carried",
      rows[0]["posted_at"] == ago(31) and rows[1]["posted_at"] == ago(13))

# 2. The other way round: the real domain owns the board and a guessed one shows up. Same answer.
conn = fresh_db()
resolved_company(conn, "at-bay.com", "greenhouse", "atbayjobs", [("7", "Actuary", ago(20), ago(21))])
load(conn, [record("atbayjobs.com", "greenhouse", "atbayjobs", [("7", "Actuary", ago(21))])])
check("a guessed domain arriving second is the one demoted",
      company(conn, "atbayjobs.com")["ats"] is None and company(conn, "at-bay.com")["ats"] == "greenhouse"
      and len(open_jobs(conn, "at-bay.com")) == 1 and open_jobs(conn, "atbayjobs.com") == [])

# 3. Neither looks guessed: the old rule holds, the resolved owner keeps the board.
conn = fresh_db()
resolved_company(conn, "exodigo.ai", "comeet", "89.005:ABC", [("e1", "Geophysicist", ago(9), ago(9))])
load(conn, [record("exodigo.com", "comeet", "89.005:ABC", [("e1", "Geophysicist", ago(9))])])
check("two real names: the one already resolved stays canonical",
      company(conn, "exodigo.ai")["ats"] == "comeet" and company(conn, "exodigo.com")["ats"] is None
      and len(open_jobs(conn, "exodigo.ai")) == 1)

# 4. Workday: the tenant is what the domain was guessed from, and its posted_at
# is a synthetic now-minus-N that the old row had already frozen.
conn = fresh_db()
resolved_company(conn, "deloitte6.com", "workday", "deloitte6:wd1:External", [("w1", "Auditor", ago(15), ago(15))])
load(conn, [record("deloitte.com", "workday", "deloitte6:wd1:External", [("w1", "Auditor", ago(0))])])
check("a workday tenant guess yields to the real domain", company(conn, "deloitte6.com")["ats"] is None)
row = open_jobs(conn, "deloitte.com")[0]
check("a workday posting keeps the frozen posted_at it had under the old domain",
      row["first_seen"] == ago(15) and row["posted_at"] == ago(15), repr(dict(row)))

# 5. The shape test itself.
check("token-shaped domains, and only those",
      all(lts._is_token_domain(d, t) for d, t in (("wix2.com", "Wix2"), ("tenableinc.com", "tenableinc"), ("Irregular.com", "Irregular"), ("tulip.co", "tulip"), ("deloitte6.com", "deloitte6:wd1:External")))
      and not any(lts._is_token_domain(d, t) for d, t in (("tenable.com", "tenableinc"), ("ridewithvia.com", "via"), ("careers.riverside.com", "66.009:X"), ("exodigo.ai", "89.005:X"))))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
