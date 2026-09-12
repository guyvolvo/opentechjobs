"""One search box.

The board used to have two, side by side, and nothing on screen said how
they differed. The left one matched a substring of the title, company,
location or department. The right one was semicolon-separated, demanded
every term, and was the only one that read the job description. Typing
"kubernetes" into one asked a different question from typing it into the
other, and a reader had no way to know which they wanted.

Now there is one: every word must appear somewhere in the listing,
quotes keep a phrase whole. Both old params still work, because saved
alerts carry them and a filter someone saved in March has to keep
meaning what it meant in March. That is what most of this file checks.

Run directly, no framework:  python tests/test_search.py
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

import job_filters  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


# id, title, company_domain, location, department, description
JOBS = [
    ("a", "Senior DevOps Engineer", "wiz.io", "Tel Aviv, Israel", "R&D",
     "You will run Kubernetes in production and own the machine learning platform."),
    ("b", "Backend Engineer", "monday.com", "Tel Aviv, Israel", "Engineering",
     "Go, Postgres, and a lot of Kubernetes."),
    ("c", "Office Manager", "wiz.io", "Haifa, Israel", "Operations", None),
    ("d", "Machine Learning Researcher", "aidoc.com", "Tel Aviv, Israel", "Research",
     "Publishing, mostly."),
]

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, company_domain TEXT, title TEXT, location TEXT,
        department TEXT, description TEXT, closed_at TEXT, confidence TEXT,
        posted_at TEXT, last_seen TEXT, skills TEXT
    )
""")
for jid, title, domain, loc, dept, desc in JOBS:
    conn.execute(
        "INSERT INTO jobs (id, company_domain, title, location, department, description,"
        " confidence, posted_at, last_seen) VALUES (?,?,?,?,?,?,'verified',"
        "'2026-09-10T00:00:00Z','2026-09-12T00:00:00Z')",
        (jid, domain, title, loc, dept, desc),
    )
conn.commit()
job_filters.register_functions(conn)


def found(params):
    where, args = job_filters.build_jobs_where(params, has_fts=False)
    return sorted(r[0] for r in conn.execute(f"SELECT id FROM jobs WHERE {where}", args))


# The four fields the old q covered, plus the one only keywords covered.
check("matches a title", found({"search": "devops"}) == ["a"])
check("matches a company", found({"search": "monday"}) == ["b"])
check("matches a location", found({"search": "haifa"}) == ["c"])
check("matches a category", found({"search": "research"}) == ["d"])
check("and matches the description, which the old main box never did",
      found({"search": "postgres"}) == ["b"], repr(found({"search": "postgres"})))

# Several words is the whole reason the second box existed.
check("every word must appear", found({"search": "kubernetes tel"}) == ["a", "b"],
      repr(found({"search": "kubernetes tel"})))
check("across different fields at once",
      found({"search": "kubernetes haifa"}) == [], repr(found({"search": "kubernetes haifa"})))
check("one word missing means no match", found({"search": "devops rust"}) == [])

# Quotes, because "machine learning" is one idea.
check("a quoted phrase stays whole",
      found({"search": '"machine learning"'}) == ["a", "d"],
      repr(found({"search": '"machine learning"'})))
check("unquoted, the same two words are two requirements",
      found({"search": "machine learning"}) == ["a", "d"])
check("and a phrase that appears in neither order matches nobody",
      found({"search": '"learning machine"'}) == [])

check("case does not matter", found({"search": "DEVOPS"}) == ["a"])
check("an empty search filters nothing", len(found({"search": "   "})) == len(JOBS))
check("no search at all filters nothing", len(found({})) == len(JOBS))

# A job with no description must not vanish from a search its title answers.
check("a null description is not a match failure", found({"search": "office"}) == ["c"])

# Saved alerts predate the single box.
check("the old q still works", found({"q": "devops"}) == ["a"])
check("the old keywords still work, still AND, still semicolons",
      found({"keywords": "kubernetes;postgres"}) == ["b"],
      repr(found({"keywords": "kubernetes;postgres"})))
check("and the two can still be combined the way an alert saved them",
      found({"q": "engineer", "keywords": "postgres"}) == ["b"])

# Tokenising.
check("whitespace of any kind splits terms",
      job_filters.search_terms("  a\tb\n c ") == ["a", "b", "c"],
      repr(job_filters.search_terms("  a\tb\n c ")))
check("quotes come off the term",
      job_filters.search_terms('"tel aviv" go') == ["tel aviv", "go"])
check("an empty quote is dropped", job_filters.search_terms('"" go') == ["go"])
check("the term count is capped",
      len(job_filters.search_terms(" ".join(str(i) for i in range(50)))) == 10)

# A search is not a way to reach other rows.
check("a closed job still stays out",
      "x" not in found({"search": "devops", "include_closed": "0"}))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
