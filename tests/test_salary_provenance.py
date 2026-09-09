"""Where a salary figure came from, carried end to end without drifting.

Three sources with different evidence behind them: the employer said it,
a hand-transcribed market table said it, or a model over real disclosed
listings said it. A reader is entitled to know which, so the flag has to
survive the loader's upsert, and it has to be readable even from a
snapshot written before the column existed.

That last part is not hypothetical. Selecting a column the live snapshot
had not gained yet took /api/jobs down for a full merge cycle once
already, so the API derives the field when the column is absent rather
than referencing it and hoping.

Run directly, no framework:  python tests/test_salary_provenance.py
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

from load_to_sqlite import load_resolved, open_db  # noqa: E402

TS = "2026-09-01T00:00:00+00:00"
failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
    if not ok:
        failures.append(f"{name}: got {got!r}, want {want!r}")


def job(salary=None, is_estimate=False, source=None, description=None) -> dict:
    return {
        "external_id": "1", "ats": "greenhouse", "title": "Backend Engineer",
        "url": "https://x/1", "location": "Tel Aviv, Israel",
        "description": description, "salary_text": salary,
        "salary_is_estimate": is_estimate, "salary_source": source,
    }


def load(*passes):
    tmp = Path(tempfile.mkdtemp())
    conn = open_db(tmp / "t.db")
    try:
        for payload in passes:
            path = tmp / "resolved.json"
            path.write_text(json.dumps([{
                "domain": "acme.com", "ats": "greenhouse", "token": "acme",
                "confidence": "verified", "job_count": 1, "jobs": [payload],
            }]), encoding="utf-8")
            with conn:
                load_resolved(conn, path)
        return conn.execute(
            "SELECT salary_text, salary_is_estimate, salary_source FROM jobs"
        ).fetchone()
    finally:
        conn.close()


row = load(job("$120K - $150K", False, "disclosed", "d"))
check("a disclosed figure is recorded as disclosed", row["salary_source"], "disclosed")
check("and is not flagged an estimate", row["salary_is_estimate"], 0)

row = load(job("₪30K–37K", True, "table", "d"))
check("a table estimate is recorded as table", row["salary_source"], "table")

row = load(job("$150K - $190K", True, "estimated", "d"))
check("a learned estimate is recorded as estimated", row["salary_source"], "estimated")
check("and is still flagged an estimate", row["salary_is_estimate"], 1)

# A probe.py that predates the field sends no source at all. Every
# estimate before the learned model came from the table, so that is
# what an unlabelled one has to mean.
row = load(job("₪30K–37K", True, None, "d"))
check("an unlabelled estimate falls back to table", row["salary_source"], "table")
row = load(job("$120K - $150K", False, None, "d"))
check("an unlabelled real figure falls back to disclosed", row["salary_source"], "disclosed")
row = load(job(None, False, None, "d"))
check("no salary means no source", row["salary_source"], None)

# The source must move with the text, in both directions, or a row ends
# up claiming an employer published a number we invented.
row = load(job("₪30K–37K", True, "table", "d"), job("$120K - $150K", False, "disclosed", None))
check("a disclosed figure overwrites the source too", row["salary_source"], "disclosed")
check("and the text with it", row["salary_text"], "$120K - $150K")

row = load(job("₪20K–100K", True, "table", "old"), job(None, False, None, "a real description"))
check("withdrawing an estimate clears its source", row["salary_source"], None)
check("and its text", row["salary_text"], None)

row = load(job("$120K - $150K", False, "disclosed", "d"), job(None, False, None, "a real description"))
check("a disclosed figure is never withdrawn", row["salary_source"], "disclosed")

# A description-less pass must not downgrade a better-informed source,
# the same guard salary_text already has.
row = load(job("$150K - $190K", True, "estimated", "d"), job("₪22K–27K", True, "table", None))
check("a blind pass does not overwrite a better source", row["salary_source"], "estimated")


# The API's fallback, against a snapshot that predates the column.
def api_fallback():
    from job_filters import salary_source_select

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (salary_text TEXT, salary_is_estimate INTEGER)")
    conn.executemany("INSERT INTO jobs VALUES (?,?)",
                     [("$120K - $150K", 0), ("₪30K–37K", 1), (None, 0), ("", 0)])
    got = [r[0] for r in conn.execute(f"SELECT {salary_source_select(conn)} FROM jobs")]
    conn.close()
    return got


check("the API derives the source when the column is missing",
      api_fallback(), ["disclosed", "table", None, None])


def api_real_column():
    from job_filters import salary_source_select

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (salary_text TEXT, salary_is_estimate INTEGER, salary_source TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('$150K - $190K', 1, 'estimated')")
    got = [r[0] for r in conn.execute(f"SELECT {salary_source_select(conn)} FROM jobs")]
    conn.close()
    return got


check("and reads the real column once it exists", api_real_column(), ["estimated"])

print()
if failures:
    print(f"{len(failures)} failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
