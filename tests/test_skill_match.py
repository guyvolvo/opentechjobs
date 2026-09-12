"""Matching a CV against the board: OR, then ranked by overlap.

This exists because the first version shipped as a single q= substring
and could never match anything. A CV with twelve skills asked the board
for the literal phrase "Python Azure Linux CI/CD Git Terraform..." and
got NO RESULTS every time, which is exactly what a search for that
phrase should return. The bug was silent: no error, no empty-vocabulary
warning, just an honest answer to the wrong question.

So the tests here are mostly about shape. Does one skill in common put a
job on the list, does the job with more in common come first, and does a
job with none of them stay off it.

Run directly, no framework:  python tests/test_skill_match.py
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


# A board just big enough to rank. The skills column is comma-joined
# canonical labels, exactly as loader/load_to_sqlite.py writes it.
JOBS = [
    ("a", "Platform Engineer", "Python,AWS,Terraform,Docker,Linux"),
    ("b", "Backend Engineer", "Python,Go"),
    ("c", "Frontend Engineer", "JavaScript,React"),
    ("d", "Office Manager", ""),
    ("e", "Data Engineer", None),
    ("f", "Go Developer", "Go,Kubernetes"),
]

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, company_domain TEXT, ats TEXT, title TEXT,
        location TEXT, department TEXT, seniority TEXT, workplace_type TEXT,
        url TEXT, posted_at TEXT, confidence TEXT, first_seen TEXT,
        last_seen TEXT, closed_at TEXT, skills TEXT, salary_text TEXT,
        salary_is_estimate INT
    )
""")
for i, (jid, title, skills) in enumerate(JOBS):
    conn.execute(
        "INSERT INTO jobs (id, company_domain, ats, title, url, posted_at, confidence,"
        " first_seen, last_seen, skills) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (jid, "x.com", "greenhouse", title, "http://x", f"2026-09-{10 - i:02d}T00:00:00Z",
         "verified", "2026-09-01T00:00:00Z", "2026-09-12T00:00:00Z", skills),
    )
conn.commit()
# The board's category filter is a registered SQL function, not a column.
job_filters.register_functions(conn)

# route_jobs opens its own connection from S3. There is no companies
# table here either, which is the deploy-skew case _has_company_column
# already degrades to NULL for.
handler.get_connection = lambda: conn


def run(params):
    return handler.route_jobs(params)


MINE = "Python,AWS,Docker,Terraform,Linux,Go"

res = run({"skills": MINE, "sort": "match"})
ids = [j["id"] for j in res["jobs"]]

check("a job sharing one skill is on the list", "b" in ids, repr(ids))
check("a job sharing none is not", "c" not in ids, repr(ids))
check("an untagged job is not", "d" not in ids and "e" not in ids, repr(ids))
check("the best overlap comes first", ids[0] == "a", repr(ids))
check("and the count comes back with it",
      res["jobs"][0]["match_score"] == 5, repr(res["jobs"][0].get("match_score")))
check("a one-skill match scores one",
      next(j["match_score"] for j in res["jobs"] if j["id"] == "f") == 1)
check("the matched skills are echoed for the board to mark",
      res["matched_skills"] == ["Python", "AWS", "Docker", "Terraform", "Linux", "Go"],
      repr(res["matched_skills"]))

# The whole reason this is not `keywords`: that param is AND-matched, so
# a twelve-skill CV would ask for the one job demanding all twelve.
check("matching is OR, so one skill is enough to appear",
      len(ids) == 3, repr(ids))

# Ranking is a sort the caller asks for by name. It cannot be a default
# that applies when no sort was given: the board always sends one, so
# such a default would never once have reached it.
by_title = run({"skills": MINE, "sort": "title"})
check("another sort still wins",
      [j["id"] for j in by_title["jobs"]] == ["b", "f", "a"],
      repr([j["title"] for j in by_title["jobs"]]))

# A label we never emit must narrow the match, not error the board out:
# these arrive from bookmarks and shared links, which outlive vocabularies.
check("an unknown skill is dropped rather than fatal",
      [j["id"] for j in run({"skills": "Python,COBOL,,Fortran", "sort": "match"})["jobs"]] == ["a", "b"])
# All of them unknown leaves no filter at all, so the board is
# unnarrowed. Same rule as every other param here: an unusable value is
# dropped rather than guessed at. Worth knowing rather than worth
# preventing, since the only way to get here is a link older than the
# vocabulary.
check("all-unknown leaves the board unfiltered rather than empty",
      len(run({"skills": "COBOL"})["jobs"]) == len(JOBS))

check("case and spacing do not matter",
      job_filters.wanted_skills({"skills": " python , AWS "}) == ["Python", "AWS"])
check("a repeat is counted once",
      job_filters.wanted_skills({"skills": "Go,go,GO"}) == ["Go"])
check("the list is capped",
      len(job_filters.wanted_skills({"skills": ",".join(job_filters.SKILL_LABELS)}))
      == job_filters.MAX_MATCH_SKILLS)

# No skills asked for: nothing about the board changes.
plain = run({})
check("without the param every job is still listed",
      len(plain["jobs"]) == len(JOBS), repr(len(plain["jobs"])))
check("and the score is a harmless zero",
      all(j["match_score"] == 0 for j in plain["jobs"]))
check("newest first, as before",
      [j["id"] for j in plain["jobs"]] == ["a", "b", "c", "d", "e", "f"],
      repr([j["id"] for j in plain["jobs"]]))

# Substring accidents the comma-wrapping exists to prevent.
conn.execute("UPDATE jobs SET skills = 'Django,Golang' WHERE id = 'c'")
check("Go does not match Golang or Django",
      "c" not in [j["id"] for j in run({"skills": "Go"})["jobs"]])

# It has to compose with the filters around it.
narrowed = run({"skills": MINE, "sort": "match", "q": "backend"})
check("it narrows alongside the other filters",
      [j["id"] for j in narrowed["jobs"]] == ["b"], repr(narrowed["jobs"]))
check("and total counts the match, not the board",
      run({"skills": MINE})["total"] == 3, repr(run({"skills": MINE})["total"]))

# sort=match with nothing to match on is a stale link, not an error.
check("asking to rank with no skills falls back to newest first",
      [j["id"] for j in run({"sort": "match"})["jobs"]] == ["a", "b", "c", "d", "e", "f"])

# Saved alerts share this translation, so the key has to be allowed
# through alert creation as well.
check("an alert may be saved against it", "skills" in handler._ALLOWED_FILTER_KEYS)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
