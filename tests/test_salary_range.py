"""The shekel range behind the board's salary filter.

salary_text is prose and cannot be filtered on, so the loader derives a
pair of numbers beside it (loader/salary_range.py) and /api/jobs
compares against those. Three things have to hold or the filter lies
about the board:

  - a range keeps both ends. Every shekel string is written "₪35K–45K",
    signed once on the low end, and the first version of the parser read
    only sign-prefixed figures and returned (35000, 35000). A listing at
    35-45 then sat outside a 40-50 search while showing a number inside
    it.
  - nothing in another currency is read at all. There is no exchange
    rate anywhere in this project and a rate written into the code would
    be a number nobody measured, sitting where a reader cannot see it.
  - a listing with no shekel figure stays on the board while the handles
    move. Nine open listings in ten have no salary (871,574 of 968,659,
    measured 2026-09-23), so a range that dropped them would answer a
    nudge of one handle by deleting most of the board.

Run directly, no framework:  python tests/test_salary_range.py
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

from job_filters import SnapshotCaps, build_jobs_where  # noqa: E402
from salary_range import monthly_ils  # noqa: E402

failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
    if not ok:
        failures.append(f"{name}: got {got!r}, want {want!r}")


print("-- reading a range --")
check("a signed low end and a bare high end keep both",
      monthly_ils("₪35K–45K"), (35000, 45000))
check("a range signed at both ends reads the same",
      monthly_ils("₪35K – ₪45K"), (35000, 45000))
check("written out in full rather than in K",
      monthly_ils("₪35,000–45,000"), (35000, 45000))
check("a single figure is its own floor and ceiling",
      monthly_ils("₪28K"), (28000, 28000))
# Ashby joins commission and equity onto the range with a bullet. Only
# the first clause is the salary; a ₪10K sign-on read as the top of the
# range moves the listing into a bracket it does not belong in.
check("an extras clause after the bullet is ignored",
      monthly_ils("₪28K–45K • Offers Equity"), (28000, 45000))

print()
print("-- periods --")
# Nothing on the board states a period today: every shekel row is
# probe.py's own table estimate and monthly by construction. This is
# here so the first Israeli employer to publish an annual range is read
# correctly rather than filed as a ₪360K/month job.
check("an annual figure becomes a month", monthly_ils("₪360K per year"), (30000, 30000))
# Turning an hourly rate into a month means choosing how many hours a
# month is, and that choice would be ours rather than the listing's.
check("an hourly rate is not read at all", monthly_ils("₪120 per hour"), None)

print()
print("-- currencies this cannot answer for --")
for text in ("$250K - $300K", "€200K - €280K", "£70K - £78K", "CA$84,000 - CA$110,250",
             "$30 – $50 per hour • Offers Equity", "Competitive", "", None):
    check(f"no numbers from {text!r}", monthly_ils(text), None)
# A string naming two currencies is ambiguous about which one the range
# is in, so it is not read rather than half-read.
check("a mixed-currency string is refused", monthly_ils("₪28K–45K and $30K"), None)

print()
print("-- the filter --")
CAPS = SnapshotCaps(salary_ils=True)


def where(**params) -> str:
    sql, _args = build_jobs_where(params, CAPS)
    return sql


def rows(**params) -> list[str]:
    """Which fixtures survive these params, by id."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE jobs (id TEXT, closed_at TEXT, confidence TEXT,
                                       posted_at TEXT, salary_min_ils INT, salary_max_ils INT)""")
    conn.executemany(
        "INSERT INTO jobs VALUES (?, NULL, 'verified', NULL, ?, ?)",
        [("low", 20000, 25000), ("mid", 30000, 40000),
         ("high", 50000, 60000), ("silent", None, None)],
    )
    sql, args = build_jobs_where({**params, "include_outdated": "1"}, CAPS)
    return sorted(r[0] for r in conn.execute(f"SELECT id FROM jobs WHERE {sql}", args))


check("a listing overlapping the floor is kept",
      rows(salary_min="35000"), ["high", "mid", "silent"])
check("and one overlapping the ceiling is too",
      rows(salary_max="35000"), ["low", "mid", "silent"])
check("both ends together keep only what the window touches",
      rows(salary_min="38000", salary_max="42000"), ["mid", "silent"])
# The whole point of the escape: the board does not empty out because
# most of it never quoted a figure.
check("a listing with no shekel figure survives any range",
      "silent" in rows(salary_min="55000"), True)
check("until the caller asks for figures only",
      rows(salary_min="35000", salary_known="1"), ["high", "mid"])
check("salary_known alone keeps everything that has a figure",
      rows(salary_known="1"), ["high", "low", "mid"])

# A malformed bound is no bound, the same degrading every other filter
# in build_jobs_where gives a value it cannot use.
check("a non-numeric bound adds no clause", "salary_max_ils" in where(salary_min="lots"), False)
check("an empty bound adds no clause", "salary_max_ils" in where(salary_min=""), False)

# A snapshot written before loader/salary_range.py existed has no such
# columns. Naming one would be a 500 on every board request; ignoring
# the filter hands back a wider answer than asked for, which is the
# right way round.
sql_old, _ = build_jobs_where({"salary_min": "30000"}, SnapshotCaps(salary_ils=False))
check("an older snapshot ignores the filter rather than naming the column",
      "salary_min_ils" in sql_old, False)

print()
print("-- the loader's own derivation --")

# The pair is not carried through upsert_job's eight ON CONFLICT
# branches; it is recomputed from salary_text after the load settles
# (see derive_salary_ranges' docstring for why). That makes the
# recompute the thing that can rot, so it is exercised against a real
# database rather than trusted.
import json  # noqa: E402
import tempfile  # noqa: E402

from load_to_sqlite import derive_salary_ranges, load_resolved, open_db  # noqa: E402


def one(title):
    return tuple(CONN.execute(
        "SELECT salary_text, salary_min_ils, salary_max_ils FROM jobs WHERE title = ?", (title,)
    ).fetchone())


def load(jobs):
    path = TMP / "resolved.json"
    path.write_text(json.dumps([{"domain": "acme.com", "ats": "greenhouse", "token": "acme",
                                 "confidence": "verified", "job_count": len(jobs), "jobs": jobs}]),
                    encoding="utf-8")
    with CONN:
        load_resolved(CONN, path)
        return derive_salary_ranges(CONN)


TMP = Path(tempfile.mkdtemp())
CONN = open_db(TMP / "t.db")
JOBS = [
    {"external_id": "1", "ats": "greenhouse", "title": "A", "url": "u1", "location": "Tel Aviv, Israel",
     "description": "d", "salary_text": "₪35K–45K", "salary_is_estimate": True, "salary_source": "table"},
    {"external_id": "2", "ats": "greenhouse", "title": "B", "url": "u2", "location": "NYC",
     "description": "d", "salary_text": "$250K - $300K", "salary_is_estimate": False, "salary_source": "disclosed"},
    {"external_id": "3", "ats": "greenhouse", "title": "C", "url": "u3", "location": "Haifa", "description": "d"},
]
load(JOBS)
check("a shekel listing gets its pair", one("A"), ("₪35K–45K", 35000, 45000))
check("a dollar one does not", one("B"), ("$250K - $300K", None, None))
check("and neither does one with no figure", one("C"), (None, None, None))

# A re-estimate rewrites salary_text in place. A pair left behind from
# the old text would filter the listing under a salary it no longer
# shows, which is why every shekel row is recomputed each pass.
JOBS[0]["salary_text"] = "₪40K–60K"
load(JOBS)
check("a re-estimate moves the pair with it", one("A"), ("₪40K–60K", 40000, 60000))

# And the clearing pass: a listing that stops being priced in shekels
# has to stop carrying shekel numbers.
JOBS[0].update(salary_text="$90K - $120K", salary_source="disclosed", salary_is_estimate=False)
load(JOBS)
check("a currency change clears the pair", one("A"), ("$90K - $120K", None, None))
CONN.close()

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
