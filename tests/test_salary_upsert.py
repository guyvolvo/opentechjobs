"""When a re-scrape is allowed to change, keep, or withdraw a stored salary.

This is the half of the estimator that can lose data. The upsert can now
write NULL over a stored value, which nothing in this pipeline could do
before, so the rules need pinning down in every direction:

  disclosed salary       -> always wins, and is never withdrawn
  estimate + description -> wins, that pass had the most to go on
  estimate, no description -> must not overwrite a better stored value
  nothing + description  -> withdraws a stored ESTIMATE, never a disclosure
  nothing, no description -> changes nothing

The fourth rule is the new one. probe.py now declines to estimate where
it used to emit something useless, and without a way to withdraw, every
listing already carrying a bad estimate would keep it until it closed.

Run directly, no framework:  python tests/test_salary_upsert.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

from load_to_sqlite import load_resolved, open_db  # noqa: E402

TS = "2026-09-01T00:00:00+00:00"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{'' if ok else '  -- ' + detail}")
    if not ok:
        failures.append(name)


def result(job: dict) -> list[dict]:
    return [{
        "domain": "acme.com", "ats": "greenhouse", "token": "acme",
        "confidence": "verified", "job_count": 1, "jobs": [job],
    }]


def job(salary=None, is_estimate=False, description=None) -> dict:
    return {
        "external_id": "1", "ats": "greenhouse", "title": "Backend Engineer", "url": "https://x/1",
        "location": "Tel Aviv, Israel", "description": description,
        "salary_text": salary, "salary_is_estimate": is_estimate,
    }


def stored(conn):
    row = conn.execute("SELECT salary_text, salary_is_estimate FROM jobs").fetchone()
    return (row[0], row[1]) if row else (None, None)


def run(first: dict, second: dict):
    """Load one job, then re-scrape it, and report what stuck."""
    tmp = Path(tempfile.mkdtemp())
    conn = open_db(tmp / "t.db")
    try:
        for payload in (first, second):
            path = tmp / "resolved.json"
            path.write_text(json.dumps(result(payload)), encoding="utf-8")
            with conn:
                load_resolved(conn, path)
        return stored(conn)
    finally:
        conn.close()


# A pass with a description is fully informed, so its estimate stands.
check("an estimate lands when the pass had a description",
      run(job(), job("₪30K–37K", True, "We use Go.")) == ("₪30K–37K", 1),
      str(run(job(), job("₪30K–37K", True, "We use Go."))))

# The Comeet problem: a fast-poll carries no description, so its estimate
# falls back to the bare title. It must not overwrite a better one.
check("a description-less estimate does not overwrite a better stored one",
      run(job("₪30K–37K", True, "We use Go."), job("₪22K–27K", True, None)) == ("₪30K–37K", 1),
      str(run(job("₪30K–37K", True, "We use Go."), job("₪22K–27K", True, None))))

# A real disclosed figure beats an estimate no matter what.
check("a disclosed salary beats a stored estimate",
      run(job("₪30K–37K", True, "desc"), job("$120K - $150K", False, None)) == ("$120K - $150K", 0),
      str(run(job("₪30K–37K", True, "desc"), job("$120K - $150K", False, None))))

# The new rule. An informed pass that declines withdraws the estimate.
check("an informed pass with no estimate withdraws the stored estimate",
      run(job("₪20K–100K", True, "old"), job(None, False, "a real description")) == (None, 0),
      str(run(job("₪20K–100K", True, "old"), job(None, False, "a real description"))))

# And the guard on it: a disclosed salary is never withdrawn this way.
check("a disclosed salary is never withdrawn",
      run(job("$120K - $150K", False, "d"), job(None, False, "a real description")) == ("$120K - $150K", 0),
      str(run(job("$120K - $150K", False, "d"), job(None, False, "a real description"))))

# A pass with nothing to say changes nothing, which is every Comeet
# fast-poll on a job whose estimate is already good.
check("a pass with neither estimate nor description changes nothing",
      run(job("₪30K–37K", True, "desc"), job(None, False, None)) == ("₪30K–37K", 1),
      str(run(job("₪30K–37K", True, "desc"), job(None, False, None))))

# Something beats nothing on a first pass, even with no description.
check("a first estimate lands even without a description",
      run(job(), job("₪22K–27K", True, None)) == ("₪22K–27K", 1),
      str(run(job(), job("₪22K–27K", True, None))))

print()
if failures:
    print(f"{len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("all passed")
