"""demote_empty_boards: a resolved company with nothing on its board is
released, and stays released through the known.json keep step.

Measured 2026-09-18: 446 tracked companies were resolved to an ATS with
no open job, 355 of them on Workable slugs that never held one, Rafael
and IAI among them. While resolved, a company's real careers site is
never looked at again.

Run directly, no framework:  python tests/test_demote_empty.py
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


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
ts = NOW.isoformat(timespec="seconds")
ago = lambda d: (NOW - timedelta(days=d)).isoformat(timespec="seconds")

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))


def company(domain, ats, token, job_count, first_seen):
    conn.execute("INSERT INTO companies (domain, ats, token, confidence, job_count, first_seen, last_checked)"
                 " VALUES (?, ?, ?, 'verified', ?, ?, ?)", (domain, ats, token, job_count, first_seen, ts))


def job(domain, jid, closed_at=None):
    conn.execute("INSERT INTO jobs (id, company_domain, ats, title, url, confidence, first_seen, last_seen, closed_at)"
                 " VALUES (?, ?, 'x', 't', 'u', 'verified', ?, ?, ?)", (jid, domain, ago(30), ago(30), closed_at))


# The four shapes that matter.
company("rafael.co.il", "workable", "rafael", 0, ago(40))          # never had a job, pinned to an empty slug
company("cellebrite.com", "workable", "cellebrite", 0, ago(90))    # had jobs, board emptied a week ago
job("cellebrite.com", "c1", closed_at=ago(7)); job("cellebrite.com", "c2", closed_at=ago(7))
company("fresh.io", "workable", "fresh", 0, ago(1))                # resolved yesterday, nothing yet: too soon
company("between.io", "greenhouse", "between", 0, ago(60))         # emptied yesterday: too soon
job("between.io", "b1", closed_at=ago(1))
company("alive.com", "greenhouse", "alive", 12, ago(60))           # a normal working board
job("alive.com", "a1")
company("stale-count.com", "lever", "stale", 0, ago(60))           # job_count 0 from a 304 run, but jobs open
job("stale-count.com", "s1")

demoted = lts.demote_empty_boards(conn, ts, days=3)

check("an empty pinned slug that never held a job is released", "rafael.co.il" in demoted, repr(demoted))
check("a board that emptied a week ago is released", "cellebrite.com" in demoted)
check("a board resolved yesterday is given time", "fresh.io" not in demoted)
check("a board that emptied yesterday is given time", "between.io" not in demoted)
check("a working board is untouched", "alive.com" not in demoted)
check("open jobs beat a stale zero job_count", "stale-count.com" not in demoted)

r = conn.execute("SELECT ats, token, error FROM companies WHERE domain = 'cellebrite.com'").fetchone()
check("released means unresolved, with the reason and the old board recorded",
      r["ats"] is None and r["token"] is None and r["error"].startswith(lts.DEMOTED_EMPTY_MARK)
      and "workable:cellebrite" in r["error"], repr(dict(r)))
check("its job history is kept",
      conn.execute("SELECT COUNT(*) FROM jobs WHERE company_domain = 'cellebrite.com'").fetchone()[0] == 2)
check("running again releases nothing new", lts.demote_empty_boards(conn, ts, days=3) == [])

# The keep step must not bring a released company straight back. A
# released company's only board is the empty one, so the sweep answers
# inconclusively for it, which is exactly the shape the keep step
# preserves for companies added mid-run.
tmp = Path(tempfile.mkdtemp())
row = lambda d, ats="workable": {"domain": d, "ats": ats, "token": d.split(".")[0]}
live = [row("rafael.co.il"), row("cellebrite.com"), row("added-midrun.com", "comeet")]
(tmp / "baseline.json").write_text(json.dumps([row("rafael.co.il"), row("cellebrite.com")]))
(tmp / "resolved.json").write_text(json.dumps([
    {"domain": "rafael.co.il", "ats": None, "retryable": True},
    {"domain": "cellebrite.com", "ats": None, "retryable": True},
]))
(tmp / "known.json").write_text(json.dumps([]))
real_pull = lts.s3_pull
lts.s3_pull = lambda bucket, key, dest: (Path(dest).write_text(json.dumps(live)), (True, "etag"))[1]
try:
    kept = lts.keep_companies_added_meanwhile("b", "known.json", tmp / "known.json",
                                              tmp / "baseline.json", tmp / "resolved.json",
                                              exclude=set(demoted))
finally:
    lts.s3_pull = real_pull
out = {e["domain"] for e in json.loads((tmp / "known.json").read_text())}
check("the keep step still keeps a company added mid-run", "added-midrun.com" in out, repr(out))
check("and does not resurrect the released ones", not ({"rafael.co.il", "cellebrite.com"} & out), repr(out))
check("reporting only what it kept", kept == 1, repr(kept))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
