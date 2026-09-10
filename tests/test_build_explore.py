"""The database the Explore page queries: shape, derived columns, pacing.

What matters here is that the derived things are right, because a user
will build queries on them and never see the source. category has to be
the same normalisation the board filters on. days_open has to mean what
its comment says for both open and closed rows. And the skills table has
to be a faithful split of the comma-joined column, or "top skills in
senior roles" quietly undercounts.

Run directly, no framework:  python tests/test_build_explore.py
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

import build_explore  # noqa: E402
from job_filters import classify_category  # noqa: E402
from load_to_sqlite import load_resolved, open_db  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def job(n, title, dept, skills=None, seniority=None):
    return {"external_id": str(n), "ats": "greenhouse", "title": title, "department": dept,
            "url": "https://acme.com/%d" % n, "location": "Tel Aviv, Israel",
            "skills": skills, "seniority": seniority, "description": "x"}


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    conn = open_db(tmp / "snap.db")
    # skills is a LIST here, matching what probe.py emits. The loader
    # joins it with commas; hand it a string and it joins the characters,
    # which is exactly the bug this fixture caught in itself first.
    jobs = [job(1, "Senior Backend Engineer", "Engineering", ["python", "aws", "kubernetes"], "senior"),
            job(2, "Account Executive", "Sales", None, None),
            job(3, "Data Scientist", "Data", ["python", "sql"], "mid")]
    path = tmp / "r.json"
    path.write_text(json.dumps([{"domain": "acme.com", "ats": "greenhouse", "token": "acme",
                                 "confidence": "verified", "job_count": 3, "jobs": jobs}]),
                    encoding="utf-8")
    load_resolved(conn, path, False)
    # One closed ten days after it was seen.
    conn.execute("""UPDATE jobs SET first_seen = ?, closed_at = ? WHERE external_id = '2'""",
                 ((datetime.now(timezone.utc) - timedelta(days=12)).isoformat(),
                  (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()))
    conn.commit()
    conn.close()

    summary = build_explore.build(tmp / "snap.db", tmp / "explore.db")
    check("build reports what it wrote", summary["jobs"] == "3" and summary["open_jobs"] == "2", str(summary))

    e = sqlite3.connect(tmp / "explore.db")
    e.row_factory = sqlite3.Row

    check("page size matches the runtime's chunk size",
          e.execute("PRAGMA page_size").fetchone()[0] == build_explore.PAGE_SIZE)

    # category is the board's own normalisation, not a second one.
    rows = {r["title"]: r for r in e.execute("SELECT * FROM jobs")}
    for title, dept in (("Senior Backend Engineer", "Engineering"), ("Account Executive", "Sales")):
        check("category for %r matches the board's classify_category()" % title,
              rows[title]["category"] == classify_category(dept, title),
              "%r vs %r" % (rows[title]["category"], classify_category(dept, title)))

    # days_open means age for open rows and duration for closed ones.
    closed = rows["Account Executive"]
    check("a closed listing's days_open is close to open minus first seen",
          closed["closed_at"] is not None and 9.5 < closed["days_open"] < 10.5, str(closed["days_open"]))
    opened = rows["Senior Backend Engineer"]
    check("an open listing's days_open is its age so far",
          opened["closed_at"] is None and 0 <= opened["days_open"] < 1, str(opened["days_open"]))

    # The skills table is a faithful split, lower-cased and trimmed.
    skills = sorted((r["job_id"], r["skill"]) for r in e.execute("SELECT * FROM job_skills"))
    by_job = {}
    for jid, s in skills:
        by_job.setdefault(jid, []).append(s)
    check("skills are split one row per term",
          sorted(by_job[opened["id"]]) == ["aws", "kubernetes", "python"], str(by_job.get(opened["id"])))
    check("a listing with no skills has no rows rather than an empty one",
          closed["id"] not in by_job, str(by_job.get(closed["id"])))
    check("the common question is a plain GROUP BY",
          e.execute("SELECT skill, COUNT(*) n FROM job_skills GROUP BY skill ORDER BY n DESC LIMIT 1").fetchone()["skill"] == "python")

    # companies carries a live open count.
    check("companies.open_jobs counts open listings only",
          e.execute("SELECT open_jobs FROM companies WHERE domain='acme.com'").fetchone()[0] == 2)

    # The pickers' values, precomputed. One field's worth is enough to
    # prove the shape; the counts have to agree with the rows they
    # summarise, or a picker would advertise listings a filter cannot find.
    facets = {(r["field"], r["value"]): r["n"] for r in e.execute("SELECT field, value, n FROM facets")}
    check("facets carry the open listings' seniorities with counts",
          facets.get(("seniority", "senior")) == 1 and facets.get(("seniority", "mid")) == 1, str(facets))
    check("a closed listing is not in the facets",
          ("seniority", None) not in facets and sum(n for (f, _), n in facets.items() if f == "seniority") == 2, str(facets))
    check("skills are faceted from open listings only",
          facets.get(("skill", "python")) == 2 and facets.get(("skill", "sql")) == 1, str(facets))
    check("companies are faceted with their display name",
          e.execute("SELECT label, n FROM facets WHERE field='company'").fetchone() is not None)
    # The builder's default question must be answerable from an index alone.
    plan = " ".join(r[3] for r in e.execute(
        "EXPLAIN QUERY PLAN SELECT category, COUNT(*) FROM jobs WHERE closed_at IS NULL GROUP BY 1"))
    check("an open-listings group-by is a covering index scan, not a table walk",
          "ix_open_category" in plan and "USING COVERING INDEX" in plan, plan)
    plan = " ".join(r[3] for r in e.execute(
        "EXPLAIN QUERY PLAN SELECT category, AVG(days_open) FROM jobs WHERE closed_at IS NOT NULL GROUP BY 1"))
    check("a closed-listings average is covered as well",
          "ix_closed_category" in plan and "USING COVERING INDEX" in plan, plan)

    # And the page can say what it is looking at.
    meta = dict(e.execute("SELECT key, value FROM meta").fetchall())
    check("meta records when and how much", "built_at" in meta and meta["jobs"] == "3", str(meta))
    check("nothing heavy came along",
          "description" not in [c[1] for c in e.execute("PRAGMA table_info(jobs)")])
    e.close()

    # Pacing, same shape as precompute's.
    class Aged:
        def __init__(self, s): self.when = datetime.now(timezone.utc) - timedelta(seconds=s)
        def head_object(self, Bucket, Key): return {"LastModified": self.when}
    class Missing:
        def head_object(self, Bucket, Key): raise RuntimeError("NoSuchKey")
    check("a recent copy is left alone", build_explore._fresh_enough(Aged(60), "b") is True)
    check("a stale copy is rebuilt", build_explore._fresh_enough(Aged(build_explore.MAX_AGE_S + 1), "b") is False)
    check("a missing copy is always built", build_explore._fresh_enough(Missing(), "b") is False)
    check("publish with no bucket is a no-op", build_explore.publish("", tmp / "snap.db", tmp) is None)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
