"""Rebuilding the search index, and what must survive if it goes wrong.

The index is contentless, so there is no recovering it from a
half-finished copy: either the old table is intact or the new one is
complete. Everything here is really one assertion, checked from several
angles, that a failure leaves the existing index searchable.

The reason this exists at all is that archive.py cannot retire a listing
whose terms it cannot remove, and the table this snapshot carries
predates the flag that makes removal possible. Rebuilding it is what
unlocks the other half of the prune.

Run directly, no framework:  python tests/test_rebuild_fts.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

import rebuild_fts  # noqa: E402
from load_to_sqlite import load_resolved, open_db  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{'' if ok else '  -- ' + detail}")
    if not ok:
        failures.append(name)


class FakeS3:
    """Serves description blobs, optionally refusing some of them."""

    def __init__(self, texts: dict, fail_ids=()):
        self.texts = texts
        self.fail_ids = set(fail_ids)

    def get_object(self, Bucket, Key):
        jid = Key.split("/")[-1].removesuffix(".json")
        if jid in self.fail_ids or jid not in self.texts:
            raise RuntimeError("NoSuchKey")
        body = json.dumps({"id": jid, "description": self.texts[jid]}).encode()
        return {"Body": type("B", (), {"read": lambda _s, b=body: b})()}


def seed(tmp: Path):
    conn = open_db(tmp / "s.db")
    jobs = [{"external_id": str(i), "ats": "greenhouse", "title": f"Engineer {i}",
             "url": f"https://acme.com/{i}", "location": "Tel Aviv, Israel",
             "description": f"kubernetes terraform listing{i}"} for i in range(5)]
    path = tmp / "r.json"
    path.write_text(json.dumps([{"domain": "acme.com", "ats": "greenhouse", "token": "acme",
                                 "confidence": "verified", "job_count": len(jobs),
                                 "jobs": jobs}]), encoding="utf-8")
    load_resolved(conn, path, False)
    conn.commit()
    texts = {r[0]: r[1] for r in conn.execute("SELECT id, description FROM jobs")}
    return conn, texts


def fts_sql(conn):
    return conn.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs_fts'").fetchone()[0]


def matches(conn, term):
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE rowid IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)",
        (term,)).fetchone()[0]


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    conn, texts = seed(tmp)

    # This fixture's SQLite may already create the table with the flag,
    # in which case the rebuild is correctly a no-op. Force the old shape
    # so there is something to actually rebuild.
    conn.execute("DROP TABLE jobs_fts")
    conn.execute("CREATE VIRTUAL TABLE jobs_fts USING fts5(description, content='')")
    for rowid, text in conn.execute("SELECT rowid, description FROM jobs").fetchall():
        conn.execute("INSERT INTO jobs_fts(rowid, description) VALUES (?, ?)", (rowid, text))
    conn.commit()
    check("the fixture starts without contentless_delete",
          "contentless_delete" not in fts_sql(conn), fts_sql(conn))
    before = matches(conn, "kubernetes")
    check("and search works on it", before == 5, str(before))

    # A blob that cannot be read must not take the whole rebuild down,
    # and must not silently drop the listing from search either: it is
    # counted so the gap is visible.
    result = rebuild_fts.rebuild(conn, "bucket", log=lambda *a: None, s3=FakeS3(texts))
    check("the table now supports row deletion",
          "contentless_delete" in fts_sql(conn), fts_sql(conn))
    check("every listing is searchable again", matches(conn, "kubernetes") == 5,
          str(matches(conn, "kubernetes")))
    check("and by a second term, so this is a real index not a stub",
          matches(conn, "terraform") == 5, str(matches(conn, "terraform")))
    check("the summary counts what it indexed", result["indexed"] == 5, str(result))

    # Now the point of the whole exercise: rows can be deleted.
    rowid = conn.execute("SELECT rowid FROM jobs LIMIT 1").fetchone()[0]
    conn.execute("DELETE FROM jobs_fts WHERE rowid = ?", (rowid,))
    check("a row can now be dropped from the index", matches(conn, "kubernetes") == 4,
          str(matches(conn, "kubernetes")))

    # Running it again changes nothing.
    check("a second run is a no-op",
          "skipped" in rebuild_fts.rebuild(conn, "bucket", log=lambda *a: None, s3=FakeS3(texts)))
    conn.close()

# A blob that fails to read leaves that listing unindexed, counted, and
# leaves every other listing searchable.
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    conn, texts = seed(tmp)
    conn.execute("DROP TABLE jobs_fts")
    conn.execute("CREATE VIRTUAL TABLE jobs_fts USING fts5(description, content='')")
    conn.commit()
    missing_id = list(texts)[0]
    result = rebuild_fts.rebuild(conn, "bucket", log=lambda *a: None,
                                 s3=FakeS3(texts, fail_ids=[missing_id]))
    check("an unreadable blob is counted, not fatal", result["missing_blobs"] == 1, str(result))
    check("and every other listing is still indexed", result["indexed"] == 4, str(result))
    check("search reflects exactly that", matches(conn, "kubernetes") == 4,
          str(matches(conn, "kubernetes")))
    conn.close()


print()
if failures:
    print(f"{len(failures)} failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
