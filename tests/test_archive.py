"""What leaves the snapshot, what must never leave it, and what happens
when S3 says no.

Nothing ever deleted a job, so this is the first code in the project
that removes a listing outright. That makes the interesting tests the
negative ones. An open listing must survive. A recently closed one must
survive, because the throughput counters and the 14-day history still
read it. And a failed archive write must leave every row exactly where
it was: a snapshot carrying too much history costs money, while a
snapshot missing listings is simply wrong.

Run directly, no framework:  python tests/test_archive.py
"""

import gzip
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

import archive  # noqa: E402
from load_to_sqlite import load_resolved, open_db  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{'' if ok else '  -- ' + detail}")
    if not ok:
        failures.append(name)


class FakeS3:
    """Enough of the client for this module, with a switch for failure."""

    def __init__(self, fail_on_put=False):
        self.objects: dict[str, bytes] = {}
        self.fail_on_put = fail_on_put

    def put_object(self, Bucket, Key, Body, **kw):
        if self.fail_on_put and Key.startswith(archive.PREFIX):
            raise RuntimeError("S3 said no")
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {"Body": type("B", (), {"read": lambda _self, b=self.objects[Key]: b})()}


def job(n: int) -> dict:
    return {"external_id": str(n), "ats": "greenhouse", "title": f"Engineer {n}",
            "url": f"https://acme.com/{n}", "location": "Tel Aviv, Israel",
            "description": f"python role {n}"}


def seed(tmp: Path):
    conn = open_db(tmp / "s.db")
    jobs = [job(i) for i in range(6)]
    path = tmp / "resolved.json"
    path.write_text(json.dumps([{"domain": "acme.com", "ats": "greenhouse", "token": "acme",
                                 "confidence": "verified", "job_count": len(jobs),
                                 "jobs": jobs}]), encoding="utf-8")
    load_resolved(conn, path, False)
    ids = [r[0] for r in conn.execute("SELECT id FROM jobs ORDER BY external_id")]
    # Two long closed, one closed yesterday, three still open.
    conn.execute("UPDATE jobs SET closed_at = datetime('now', '-90 days') WHERE id IN (?,?)", ids[:2])
    conn.execute("UPDATE jobs SET closed_at = datetime('now', '-1 days') WHERE id = ?", (ids[2],))
    conn.commit()
    return conn, ids


def counts(conn):
    return (conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM jobs_fts").fetchone()[0])


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    # The failure path first, because it is the one that loses data.
    conn, ids = seed(tmp)
    before = counts(conn)
    result = archive.prune(conn, FakeS3(fail_on_put=True), "b", 30, True)
    check("a failed archive write deletes nothing",
          counts(conn) == before, f"{before} -> {counts(conn)}")
    check("and reports that it archived nothing", result["archived"] == 0, str(result))
    conn.close()

    # The ordinary path.
    conn, ids = seed(tmp / "ok" if (tmp / "ok").mkdir() or True else tmp)
    s3 = FakeS3()
    total_before, open_before, fts_before = counts(conn)
    result = archive.prune(conn, s3, "b", 30, True)
    total, still_open, fts = counts(conn)

    check("both long-closed listings are archived", result["archived"] == 2, str(result))
    check("and are gone from the snapshot", total == total_before - 2, f"{total_before} -> {total}")
    check("open listings are untouched", still_open == open_before, f"{open_before} -> {still_open}")
    check("the recently closed one stays",
          conn.execute("SELECT COUNT(*) FROM jobs WHERE id = ?", (ids[2],)).fetchone()[0] == 1)
    check("their FTS rows go too, so keyword search cannot resurrect them",
          fts == fts_before - 2, f"{fts_before} -> {fts}")

    # The archive object has to be readable, and complete.
    keys = [k for k in s3.objects if k.startswith(archive.PREFIX)]
    check("one archive object per closing month", len(keys) == 1, str(keys))
    records = [json.loads(l) for l in gzip.decompress(s3.objects[keys[0]]).decode().splitlines()]
    check("every archived row is in it", len(records) == 2, str(len(records)))
    check("archived rows keep the fields a reader would need",
          all({"id", "title", "company_domain", "closed_at", "first_seen"} <= set(r) for r in records),
          str(sorted(records[0])))
    check("and not the description, which lives in its own object",
          all("description" not in r for r in records), str(sorted(records[0])))

    # Pacing.
    check("a fresh marker holds the next run off", archive.due(s3, "b") is False)
    check("a missing marker does not", archive.due(FakeS3(), "b") is True)

    # The regression that cost the applier 18 seconds on every run: a
    # prune that found nothing skipped the marker, so due() stayed true
    # and the full scan repeated every five minutes instead of daily.
    empty = FakeS3()
    conn2, _ = seed(tmp / "nowt" if (tmp / "nowt").mkdir() or True else tmp)
    conn2.execute("UPDATE jobs SET closed_at = NULL")
    conn2.commit()
    result = archive.prune(conn2, empty, "b", 30, True)
    check("a prune that finds nothing still records that it ran",
          result["archived"] == 0 and archive.due(empty, "b") is False, str(result))
    conn2.close()

    # Idempotent: nothing left to take.
    again = archive.prune(conn, s3, "b", 30, True)
    check("a second pass finds nothing and writes nothing",
          again["archived"] == 0 and again["objects"] == 0, str(again))

    conn.close()

# Where the runtime cannot drop an index entry, the entry outlives the
# row. That is inert only if a freed rowid can never be handed to a new
# listing, which is what the MAX(rowid) guard below guarantees. Without
# it, a new listing would inherit a retired one's words and be findable
# by text it does not contain.
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    conn, ids = seed(tmp)
    result = archive.prune(conn, FakeS3(), "b", 30, False)
    check("rows are retired even when their index entry cannot be",
          result["archived"] == 2, str(result))
    check("and the entries left behind are counted, not hidden",
          result["orphaned_index_rows"] >= 0, str(result))

    highest = conn.execute("SELECT MAX(rowid) FROM jobs").fetchone()[0]
    check("the highest rowid is still present, so the counter cannot rewind",
          conn.execute("SELECT COUNT(*) FROM jobs WHERE rowid = ?", (highest,)).fetchone()[0] == 1)

    # The actual property: a listing inserted after a prune must never
    # receive a rowid that a retired listing used.
    retired = {r[0] for r in conn.execute("SELECT id FROM jobs_fts_docsize WHERE id NOT IN (SELECT rowid FROM jobs)")}
    conn.execute("INSERT INTO jobs (id, company_domain, ats, title, confidence, first_seen, last_seen) "
                 "VALUES ('fresh-1', 'acme.com', 'greenhouse', 'New Role', 'verified', "
                 "datetime('now'), datetime('now'))")
    new_rowid = conn.execute("SELECT rowid FROM jobs WHERE id = 'fresh-1'").fetchone()[0]
    check("a listing added after a prune cannot land on a freed rowid",
          new_rowid not in retired, f"{new_rowid} in {sorted(retired)}")
    check("and it is above every rowid ever issued", new_rowid > highest,
          f"{new_rowid} vs {highest}")
    conn.close()

# The guard has to hold even when the prune would otherwise take
# everything, which is the case that could empty the table and restart
# the counter at 1.
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    conn, ids = seed(tmp)
    conn.execute("UPDATE jobs SET closed_at = datetime('now', '-90 days')")
    conn.commit()
    before_max = conn.execute("SELECT MAX(rowid) FROM jobs").fetchone()[0]
    archive.prune(conn, FakeS3(), "b", 30, False)
    left = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    check("one row always survives, so the rowid counter never restarts",
          left == 1, str(left))
    check("and it is the one that held the maximum",
          conn.execute("SELECT MAX(rowid) FROM jobs").fetchone()[0] == before_max)
    conn.close()

print()
if failures:
    print(f"{len(failures)} failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
