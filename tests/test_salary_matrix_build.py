"""The matrix build: what it learns from, and what it refuses to publish.

Two failure modes matter more than accuracy here. Training on our own
estimates would turn a table of medians into a table of nothing within a
few days, since every cycle would feed yesterday's guesses back in as
evidence. And publishing a matrix built from a handful of rows would put
confident-looking numbers behind cells that never had the data, which is
the exact failure the Israeli table already committed.

Run directly, no framework:  python tests/test_salary_matrix_build.py
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "loader"))

from build_salary_matrix import build, dedupe, load_rows  # noqa: E402
from load_to_sqlite import open_db  # noqa: E402

failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
    if not ok:
        failures.append(f"{name}: got {got!r}, want {want!r}")


def snapshot(jobs):
    """jobs is [(salary_text, is_estimate, closed_at)]."""
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "snap.db"
    conn = open_db(path)
    with conn:
        conn.execute(
            "INSERT INTO companies (domain, ats, token, confidence, job_count, tried, first_seen, last_checked)"
            " VALUES ('acme.com','ashby','acme','verified',1,1,'2026-09-01','2026-09-01')")
        for i, (salary, is_estimate, closed) in enumerate(jobs):
            conn.execute(
                "INSERT INTO jobs (id, company_domain, ats, external_id, title, location, seniority,"
                " salary_text, salary_is_estimate, closed_at, confidence, first_seen, last_seen)"
                " VALUES (?, 'acme.com', 'ashby', ?, 'Engineer', 'San Francisco', 'senior',"
                " ?, ?, ?, 'verified', '2026-09-01', '2026-09-01')",
                (f"j{i}", str(i), salary, int(is_estimate), closed))
    conn.close()
    return path


# Only real disclosed pay is evidence. An estimate is our own output.
path = snapshot([
    ("$180K - $220K", False, None),
    ("₪30K–37K", True, None),          # our estimate, must not train on it
    ("$190K - $210K", False, "2026-09-02"),  # closed, no longer the market
    ("Competitive", False, None),      # unparseable
])
rows = load_rows(path)
check("only parseable, open, disclosed rows are learned from", len(rows), 1)
check("and it is the right one", rows[0][1], 200_000.0)

# A company posting one range across many reqs is one data point about
# the market, not eight.
same = [({"company_domain": "acme.com"}, 200_000.0, "$") for _ in range(8)]
other = [({"company_domain": "acme.com"}, 250_000.0, "$")]
check("duplicate figures from one company collapse", len(dedupe(same + other)), 2)
check("a different company keeps its own identical figure",
      len(dedupe(same + [({"company_domain": "other.com"}, 200_000.0, "$")])), 2)
check("the same figure in another currency is a different row",
      len(dedupe(same + [({"company_domain": "acme.com"}, 200_000.0, "£")])), 2)


def rows_for(currency, n, pay=200_000.0):
    return [({"company_domain": f"c{i}.com", "location": "San Francisco",
              "seniority": "senior", "department": "Engineering"}, pay + i * 1000, currency)
            for i in range(n)]


matrix = build(rows_for("$", 250) + rows_for("£", 40))
check("a currency with enough rows is published", "$" in matrix["models"], True)
check("a thin currency is dropped rather than guessed at", "£" in matrix["models"], False)
check("the build stamps when it ran", "built_at" in matrix, True)

check("a matrix with no qualifying currency is empty, not partial",
      build(rows_for("£", 40))["models"], {})

print()
if failures:
    print(f"{len(failures)} failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
