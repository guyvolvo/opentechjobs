"""Whether --drop-description actually leaves the column empty.

It did not, for as long as the S3 description store has existed. The
clear only ever ran over rows whose text had CHANGED in that pass, while
upsert_job writes the column for every job in the payload. So each poll
of a company refilled the column for all of its unchanged listings, and
the file the migration was meant to shrink grew instead: 60,396 rows
holding 337MB on 2026-09-10, every byte of it already in S3, re-uploaded
and re-versioned every five minutes.

The rule now is that the column ends the run empty. The exception is a
row whose upload failed, which keeps its text and drops its stored hash
so the next poll retries it. Without that, a single bad PUT would turn
into a description nobody can get back without a re-scrape.

Run directly, no framework:  python tests/test_description_drop.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

import load_to_sqlite  # noqa: E402
from load_to_sqlite import load_resolved, open_db  # noqa: E402

TS = "2026-09-01T00:00:00+00:00"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{'' if ok else '  -- ' + detail}")
    if not ok:
        failures.append(name)


def result(jobs: list[dict]) -> list[dict]:
    return [{
        "domain": "acme.com", "ats": "greenhouse", "token": "acme",
        "confidence": "verified", "job_count": len(jobs), "jobs": jobs,
    }]


def job(n: int, description: str) -> dict:
    return {
        "external_id": str(n), "ats": "greenhouse", "title": f"Engineer {n}",
        "url": f"https://acme.com/{n}", "location": "Tel Aviv, Israel",
        "description": description,
    }


def run(conn, jobs, tmp, uploads_land=True):
    """One load pass, with S3 stubbed. Returns the ids handed to put_many."""
    seen: list[str] = []

    def fake_put_many(bucket, items):
        seen.extend(jid for jid, _ in items)
        return {jid for jid, _ in items} if uploads_land else set()

    original = load_to_sqlite.put_many
    load_to_sqlite.put_many = fake_put_many
    load_to_sqlite.DESCRIPTIONS_BUCKET = "test-bucket"
    try:
        path = tmp / "resolved.json"
        path.write_text(json.dumps(result(jobs)), encoding="utf-8")
        load_resolved(conn, path, True)
        conn.commit()
    finally:
        load_to_sqlite.put_many = original
    return seen


def column(conn):
    return {r["id"]: r["description"] for r in conn.execute("SELECT id, description FROM jobs")}


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    conn = open_db(tmp / "t.db")

    # First pass: three new listings, all uploaded, all cleared.
    run(conn, [job(1, "alpha text"), job(2, "beta text"), job(3, "gamma text")], tmp)
    check("a first load leaves no text in the column",
          all(v is None for v in column(conn).values()), str(column(conn)))

    # Second pass, one description changed. The other two are unchanged,
    # which is the case that used to refill the column.
    uploaded = run(conn, [job(1, "alpha text"), job(2, "beta CHANGED"), job(3, "gamma text")], tmp)
    check("only the changed description is uploaded again",
          len(uploaded) == 1, f"uploaded {uploaded}")
    check("unchanged rows do not get their text written back",
          all(v is None for v in column(conn).values()), str(column(conn)))

    # A failed upload is the one case where the column is the last copy.
    run(conn, [job(1, "alpha REWRITTEN")], tmp, uploads_land=False)
    rows = {r["id"]: (r["description"], r["description_sha"])
            for r in conn.execute("SELECT id, description, description_sha FROM jobs")}
    kept = [d for d, _ in rows.values() if d == "alpha REWRITTEN"]
    check("a failed upload keeps its text rather than losing it",
          kept == ["alpha REWRITTEN"], str(rows))
    check("a failed upload clears its hash so the next poll retries",
          any(d == "alpha REWRITTEN" and sha is None for d, sha in rows.values()), str(rows))

    # And the retry drains it, so a failure is a delay and not a leak.
    run(conn, [job(1, "alpha REWRITTEN")], tmp)
    check("the retry clears the column once the upload lands",
          all(v is None for v in column(conn).values()), str(column(conn)))

    # Windows will not remove the temp dir while the handle is open.
    conn.close()

print()
if failures:
    print(f"{len(failures)} failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
