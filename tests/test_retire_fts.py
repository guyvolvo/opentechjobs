"""Whether retiring the search index actually retires it.

Dropping jobs_fts did not survive the next load. open_db() applies
db/schema.sql on every open and its CREATE VIRTUAL TABLE says IF NOT
EXISTS, so the table came back empty and index_description refilled it
with whichever listings changed since. Measured on the live snapshot a
day after the drop on 2026-09-22: 234MB of index over 130,740 of
1,007,133 rows, and the API reported description searches over 13% of
the board as if they covered all of it.

So the decision is recorded on the file, not in the caller: retire_fts()
writes a marker that open_db() reads, and a run that builds a real index
(loader/fts_full.py) clears it. The rest of the loader has to survive
the table being absent, which is what most of this file checks.

Run directly, no framework:  python tests/test_retire_fts.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

import load_to_sqlite  # noqa: E402
from load_to_sqlite import load_resolved, open_db, retire_fts  # noqa: E402

TS = "2026-09-01T00:00:00+00:00"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{'' if ok else '  -- ' + detail}")
    if not ok:
        failures.append(name)


def has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,)
    ).fetchone() is not None


def payload(title: str, description: str) -> list[dict]:
    return [{
        "domain": "example.com", "ats": "greenhouse", "token": "example",
        "confidence": "verified", "checked_at": TS,
        "jobs": [{"external_id": "1", "ats": "greenhouse", "title": title,
                  "url": "https://example.com/1", "location": "Tel Aviv, Israel",
                  "department": "Engineering", "posted_at": TS,
                  "description": description, "description_chars": len(description)}],
    }]


def write(tmp: Path, jobs: list[dict]) -> Path:
    path = tmp / "resolved.json"
    path.write_text(json.dumps(jobs), encoding="utf-8")
    return path


with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    # No bucket: descriptions stay in the column, which is what a local
    # run does anyway, and none of this is about where the text lives.
    load_to_sqlite.DESCRIPTIONS_BUCKET = None
    db = tmp / "jobs.db"

    conn = open_db(db)
    with conn:
        load_resolved(conn, write(tmp, payload("Rust Engineer", "we use rust and kubernetes")))
    check("a fresh database carries the index", has_table(conn, "jobs_fts"))
    indexed = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH 'kubernetes')"
    ).fetchone()[0]
    check("and the load indexed into it", indexed == 1, f"{indexed} rows matched")

    freed = retire_fts(conn)
    conn.commit()
    check("retire_fts drops the table", not has_table(conn, "jobs_fts"))
    check("and frees its pages", freed > 0, f"{freed} pages")
    check("and clears any completeness claim",
          conn.execute("SELECT COUNT(*) FROM meta WHERE key = 'fts_complete'").fetchone()[0] == 0)
    conn.close()

    # The whole point: the next open must not put it back.
    conn = open_db(db)
    check("reopening does not recreate it", not has_table(conn, "jobs_fts"))

    # And a load against a file with no index must not raise. Same
    # payload with different text, so the path that would have
    # re-indexed is the one being exercised.
    try:
        with conn:
            load_resolved(conn, write(tmp, payload("Rust Engineer", "now we use terraform as well")))
        ok, detail = True, ""
    except Exception as e:  # noqa: BLE001 -- the failure being tested for
        ok, detail = False, repr(e)
    check("a load with no index applies cleanly", ok, detail)
    check("and the listing is still there and current",
          conn.execute("SELECT description FROM jobs").fetchone()[0] == "now we use terraform as well")
    check("and the index did not come back", not has_table(conn, "jobs_fts"))

    # The API's own read of the same file.
    from job_filters import has_fts_index  # noqa: E402

    caps = has_fts_index(conn)
    check("the API sees no usable index", not caps.fts)

    # An index that exists but was never declared complete is the state
    # the live snapshot was in, and it must not be trusted either.
    conn.execute("CREATE VIRTUAL TABLE jobs_fts USING fts5(description, content='')")
    conn.execute("DELETE FROM meta WHERE key = 'fts_retired'")
    conn.commit()
    import job_filters  # noqa: E402

    job_filters._caps_cache.clear()
    caps = has_fts_index(conn)
    check("a partial index is not trusted either", not caps.fts)

    job_filters._caps_cache.clear()
    conn.execute("INSERT INTO meta (key, value) VALUES ('fts_complete', '1')"
                 " ON CONFLICT(key) DO UPDATE SET value = '1'")
    conn.commit()
    caps = has_fts_index(conn)
    check("one marked complete is", caps.fts)
    check("and a single-column one is not treated as the five-column index", not caps.fts_full)
    conn.close()

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
