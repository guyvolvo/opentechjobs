"""Every query the builder can generate, executed against a real explore.db.

test_qb.js checks the SQL's shape. This checks that SQLite accepts it,
which is the property that matters: a builder producing plausible SQL
that does not parse is worse than none. Runs node with --emit, collects
each generated query, and executes it read-only.

Uses the local explore.db built from the live snapshot when present, and
otherwise builds a small one from a fixture so the test never silently
skips.

Run directly, no framework:  python tests/test_qb_runs.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def fixture_db(tmp: Path) -> Path:
    import build_explore
    from load_to_sqlite import load_resolved, open_db

    conn = open_db(tmp / "snap.db")
    jobs = [{"external_id": str(i), "ats": "greenhouse", "title": "Engineer %d" % i,
             "department": "Engineering", "url": "https://acme.com/%d" % i,
             "location": "Tel Aviv, Israel", "skills": ["python"], "seniority": "senior",
             "description": "x"} for i in range(5)]
    p = tmp / "r.json"
    p.write_text(json.dumps([{"domain": "acme.com", "ats": "greenhouse", "token": "acme",
                              "confidence": "verified", "job_count": 5, "jobs": jobs}]),
                 encoding="utf-8")
    load_resolved(conn, p, False)
    conn.commit()
    conn.close()
    build_explore.build(tmp / "snap.db", tmp / "explore.db")
    return tmp / "explore.db"


with tempfile.TemporaryDirectory() as td:
    real = Path(os.environ.get("EXPLORE_DB", "")) if os.environ.get("EXPLORE_DB") else None
    db = real if real and real.exists() else fixture_db(Path(td))
    print("  against:", "live-snapshot build" if db == real else "fixture build")

    out = subprocess.run(["node", str(ROOT / "tests" / "test_qb.js"), "--emit"],
                         capture_output=True, text=True)
    check("the structural tests pass first", out.returncode == 0, out.stdout[-400:])
    queries = [l[4:] for l in out.stdout.splitlines() if l.startswith("SQL\t")]
    check("the builder emitted queries to run", len(queries) >= 30, str(len(queries)))

    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    bad = []
    for q in queries:
        try:
            conn.execute(q).fetchall()
        except sqlite3.Error as e:
            bad.append("%s\n    %s" % (e, q[:160]))
    check("every generated query executes on a real explore.db",
          not bad, "\n  " + "\n  ".join(bad[:5]))
    conn.close()

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
