"""Relevance weighs age. Whole-word matching does not, any more.

REVERTED 2026-09-22, the same day it shipped. The whole-word rule below
made the site return HTTP 500 on any common search term: measured live
against 805,863 jobs, search=python timed the Lambda out at 29 seconds
on both sorts. A search for "rust" returns "Trust Officer" again, and
that is recorded here as a debt rather than quietly dropped. The fix
belongs in FTS5; see the note at the top of api/job_filters.py.

Original note follows, because the reasoning still stands.

A search for "rust" is not a search for "Trust Officer".

Reported live 2026-09-22 against the production API. Sorting by
relevance, `search=rust` returned, in order: two genuine Rust roles,
then "Lead Product Manager, Trustly Remember Me", "Sr Customer Success
Manager EMEA - Entrust Identity", "Platform Architect - Agentic Trust",
and a 259-day-old "Begeleider - Buitenrust". Every field was matched
with LIKE '%rust%', which has no idea where a word starts.

The same session found the other half of the complaint: relevance put a
62-day-old listing above two posted that morning. Measured on "machine
learning engineer", the older ones scored 115 and the fresh ones 110,
and the entire gap was a department reading "Machine Learning
Engineering" rather than "Machine Learning" -- five points for the word
"engineer". Freshness was only a tiebreaker between identical scores,
and at that granularity identical scores barely happen.

Run directly, no framework:  python tests/test_search_words.py
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
# handler builds an S3 client at import time and botocore will go
# looking for real credentials without these.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import job_filters  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


NOW = datetime.now(timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, company_domain TEXT, title TEXT, location TEXT,
        department TEXT, description TEXT, closed_at TEXT, confidence TEXT,
        posted_at TEXT, first_seen TEXT, last_seen TEXT, skills TEXT, ats TEXT
    )
""")
job_filters.register_functions(conn)

# The six rows the live search actually returned, plus the two it should
# have returned alone.
ROWS = [
    ("r1", "scaleops.com", "Senior Staff Rust Developer", "Tel Aviv", "R&D", 3.6),
    ("r2", "wiz.io", "Principal Software Development Engineer - Rust", "Remote", "R&D", 3.6),
    ("n1", "trustly.com", "Lead Product Manager, Trustly Remember Me", "Stockholm", "Product", 18.8),
    ("n2", "entrust.com", "Sr Customer Success Manager EMEA - Entrust Identity", "Remote", "Sales", 30.7),
    ("n3", "agentic.com", "Platform Architect - Agentic Trust", "Remote", "R&D", 30.7),
    ("n4", "seo.nl", "Team Lead SEO, GEO & Digital Trust (f/m/d)", "Amsterdam", "Marketing", 64.0),
    ("n5", "zorg.nl", "Begeleider - Buitenrust", "Utrecht", "Care", 259.1),
    # Deliberately not industry.com: the dot is a boundary, so that
    # domain really does contain the word and would prove nothing.
    ("n6", "haifaworks.com", "Industrial Engineer", "Haifa", "Operations", 5.0),
]
for jid, domain, title, loc, dept, age in ROWS:
    conn.execute(
        "INSERT INTO jobs (id, company_domain, title, location, department, description,"
        " confidence, posted_at, first_seen, last_seen, ats)"
        " VALUES (?,?,?,?,?,'','verified',?,?,?,'greenhouse')",
        (jid, domain, title, loc, dept, ago(age), ago(age), ago(0)))
conn.commit()


def found(search, **extra):
    params = {"search": search, **extra}
    where, args = job_filters.build_jobs_where(params)
    sql = f"SELECT id FROM jobs WHERE {where}"
    return [r["id"] for r in conn.execute(sql, args)]


hits = found("rust")
check("a search for rust finds the Rust roles", set(hits) >= {"r1", "r2"}, repr(hits))
# The known-bad behaviour, asserted so it is a recorded debt rather than
# a surprise. The whole-word rule that fixed this was reverted the same
# day for cost (see the note at the top of job_filters.py); until it
# comes back through FTS5, a search for rust really does return Trustly
# and Buitenrust.
check("KNOWN: Trustly, Entrust and Buitenrust still come back",
      {"n1", "n2", "n5"} <= set(hits), repr(sorted(hits)))

# The boundary checks that lived here (whole-word matching, C++ and C#
# kept intact, GLOB metacharacters declined) went with the revert. They
# belong with the FTS5 work, not here, because asserting them against
# the current code would just fail.

# Age, in the ranking rather than only in the tiebreak. The live shape,
# reproduced: five listings a department-match ahead of two posted this
# morning. Ordering here uses the same expression route_jobs builds, so
# this fails if that expression is dropped or reweighted past the point
# where a fortnight is worth about one field.
import handler  # noqa: E402

conn.execute("DELETE FROM jobs")
ML = [
    ("old1", "Staff Machine Learning Engineer", "Machine Learning & Data", 6.8),
    ("old2", "Senior Machine Learning Engineer", "Machine Learning Engineering", 25.7),
    ("old3", "Senior Machine Learning Engineer I", "Machine Learning Engineering", 61.7),
    ("new1", "Machine Learning Engineer", "Machine Learning", 0.8),
    ("new2", "Machine Learning Engineer", "Machine Learning", 0.8),
]
for jid, title, dept, age in ML:
    conn.execute(
        "INSERT INTO jobs (id, company_domain, title, location, department, description,"
        " confidence, posted_at, first_seen, last_seen, ats)"
        " VALUES (?,'co.com',?,'Remote',?,'','verified',?,?,?,'greenhouse')",
        (jid, title, dept, ago(age), ago(age), ago(0)))
conn.commit()

rank_sql, rank_args = job_filters.relevance_score_sql({"search": "machine learning engineer"})
raw = {r["id"]: r["s"] for r in conn.execute(f"SELECT id, {rank_sql} AS s FROM jobs", rank_args)}
# The gap that caused the complaint has gone at its root: the five
# points came from the word "engineer" matching a department called
# "Machine Learning Engineering", and it no longer does.
# With substring matching back, "engineer" scores against a department
# called "Machine Learning Engineering" again, which is the gap that
# caused the original complaint. The age penalty is what answers it now.
check("the department gap is back, and is what the age penalty has to cross",
      raw["old2"] > raw["new1"], repr({k: raw[k] for k in ("old2", "new1")}))

age_steps = (f"CAST(MAX(0, julianday('now') - julianday(COALESCE(posted_at, first_seen)))"
             f" / {handler.RELEVANCE_RECENCY_DAYS} AS INTEGER)")
order = f"({rank_sql} - {handler.RELEVANCE_AGE_PENALTY} * {age_steps}) DESC, datetime(posted_at) DESC, id DESC"
ranked = [r["id"] for r in conn.execute(f"SELECT id FROM jobs ORDER BY {order}", rank_args)]
check("this morning's listing now outranks the 62-day-old one",
      ranked.index("new1") < ranked.index("old3"), repr(ranked))
check("and the 26-day-old one, which was two points of department ahead",
      ranked.index("new1") < ranked.index("old2"), repr(ranked))
check("and with the scores level, the two posted this morning lead",
      ranked[:2] == ["new2", "new1"], repr(ranked))
check("the rest fall in date order behind them",
      ranked[2:] == ["old1", "old2", "old3"], repr(ranked))

# The penalty has to stay small enough that a real phrase match survives
# it, or relevance quietly becomes date order.
steps_to_burn_a_phrase = job_filters.RELEVANCE_PHRASE_BONUS / handler.RELEVANCE_AGE_PENALTY
check("a whole-phrase title match outlives a weaker fresh one for months",
      steps_to_burn_a_phrase * handler.RELEVANCE_RECENCY_DAYS >= 90,
      f"{steps_to_burn_a_phrase * handler.RELEVANCE_RECENCY_DAYS:.0f} days")
check("but the penalty is not so small it cannot cross one field match",
      handler.RELEVANCE_AGE_PENALTY >= job_filters.RELEVANCE_WEIGHTS["department"],
      repr(handler.RELEVANCE_AGE_PENALTY))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
