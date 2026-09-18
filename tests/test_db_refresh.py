"""api/db.py: the snapshot refreshes in the background, never under a
request, and a bad download never replaces a good snapshot.

S3 is faked with small real SQLite files. The fake download can be held
open, which is how "a request arrives while the refresh runs" is tested.

Run directly, no framework:  python tests/test_db_refresh.py
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
os.environ.setdefault("DATA_BUCKET", "test-bucket")
os.environ.setdefault("DATA_KEY", "jobs-read.db")
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
# Dummy credentials, so creating the client never looks at this machine's
# own AWS profile. Nothing here talks to AWS.
os.environ["AWS_ACCESS_KEY_ID"] = "test"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
os.environ.pop("AWS_PROFILE", None)

import db  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


WORK = Path(tempfile.mkdtemp())


def make_snapshot(label, jobs=3, tables=("jobs", "companies", "meta"), pad=0):
    path = WORK / f"src-{label}.db"
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    if "jobs" in tables:
        conn.execute("CREATE TABLE jobs (id TEXT, title TEXT)")
        conn.executemany("INSERT INTO jobs VALUES (?, ?)", [(f"{label}-{i}", label) for i in range(jobs)])
    if "companies" in tables:
        conn.execute("CREATE TABLE companies (domain TEXT)")
    if "meta" in tables:
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    if pad:
        conn.execute("CREATE TABLE pad (b BLOB)")
        conn.execute("INSERT INTO pad VALUES (?)", (b"x" * pad,))
    conn.commit()
    conn.close()
    return path


class FakeS3:
    """One object. head_object answers with whatever is published now;
    download_file copies it, optionally waiting on a gate first, and can
    publish something else mid-download to imitate a merge landing."""

    def __init__(self):
        self.etag, self.src = None, None
        self.gate = None
        self.downloads = 0
        self.swap_during_download = None
        self.short_write = False

    def publish(self, etag, src):
        self.etag, self.src = f'"{etag}"', src

    def head_object(self, Bucket, Key):
        return {"ETag": self.etag, "ContentLength": os.path.getsize(self.src)}

    def download_file(self, Bucket, Key, Filename, Config=None, Callback=None):
        self.downloads += 1
        src = self.src
        gate = self.gate
        if gate is not None:
            gate.wait(10)
        data = Path(src).read_bytes()
        if self.short_write:
            data = data[: len(data) // 2]
        Path(Filename).write_bytes(data)
        if Callback:
            Callback(len(data))
        if self.swap_during_download:
            self.publish(*self.swap_during_download)
            self.swap_during_download = None


def reset(fake):
    with db._lock:
        for conn, _, _ in db._retired:
            conn.close()
        db._retired.clear()
        if db._conn is not None:
            db._conn.close()
        db._conn = db._path = db._etag = db._loaded_at = None
        db._last_checked = 0.0
        db._refresh.update(state="idle", etag=None, started_at=None, finished_at=None,
                           seconds=None, error=None, failures=0, retry_at=0.0, ready=None,
                           bytes=0, size=None, started_mono=None, thread=None, last_join=0.0)
        db._refresh["gen"] += 1
    db._s3 = fake
    tmp = WORK / "tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    db.TMP_DIR = str(tmp)


def title(conn):
    return conn.execute("SELECT title FROM jobs LIMIT 1").fetchone()[0]


def wait_for(pred, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def expire_recheck():
    db._last_checked = time.monotonic() - db.S3_RECHECK_SECONDS - 1


db.register_functions = lambda conn: None
v1, v2, v3 = make_snapshot("v1"), make_snapshot("v2"), make_snapshot("v3")

try:
    # Cold start downloads in the request, because there is nothing else.
    fake = FakeS3()
    reset(fake)
    fake.publish("e1", v1)
    conn = db.get_connection()
    check("cold start serves the snapshot", title(conn) == "v1")
    check("cold start downloaded once", fake.downloads == 1, str(fake.downloads))
    check("no half-downloaded file is left", not list(Path(db.TMP_DIR).glob("*.part")))

    # A cold start whose object changes mid-download tries again.
    reset(FakeS3())
    cold = db._s3
    cold.publish("c1", v1)
    cold.swap_during_download = ("c2", v2)
    conn = db.get_connection()
    check("a cold start retries when a merge lands mid-download",
          title(conn) == "v2" and cold.downloads == 2, f"{title(conn)} after {cold.downloads}")
    check("and does not report its bytes as refresh progress", db.status()["refresh"]["bytes"] == 0)
    reset(fake)
    fake.publish("e1", v1)
    db.get_connection()

    # A new ETag starts a background download; requests keep the old one.
    fake.publish("e2", v2)
    fake.gate = threading.Event()
    before_downloads = fake.downloads
    expire_recheck()
    started = time.monotonic()
    conn = db.get_connection()
    check("the request that notices a new snapshot is not held up",
          time.monotonic() - started < 1 and title(conn) == "v1")
    check("a refresh is running", db.status()["refresh"]["state"] == "downloading")
    for _ in range(20):
        expire_recheck()
        check_conn = db.get_connection()
    check("requests during the download get the old snapshot", title(check_conn) == "v1")
    check("twenty checks start one download", fake.downloads == before_downloads + 1, str(fake.downloads))

    fake.gate.set()
    check("the refresh finishes", wait_for(lambda: db.status()["refresh"]["state"] == "ready"))
    old = db._conn
    conn = db.get_connection()
    check("the next request swaps to the new snapshot", title(conn) == "v2")
    check("status reports the new ETag", db.status()["etag"] == "e2", repr(db.status()))
    check("the old connection is not closed under a request that may still hold it",
          title(old) == "v1")
    old_path = db._retired[0][1]
    db._retired[0] = (db._retired[0][0], old_path, time.monotonic() - db.RETIRE_AFTER_SECONDS - 1)
    db.get_connection()
    closed = False
    try:
        old.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        closed = True
    check("the old connection is closed once retired long enough", closed)
    check("and its file is deleted", not os.path.exists(old_path))

    # The object changes during the download: parts may be mixed, discard.
    fake.gate = None
    fake.publish("e3", v3)
    fake.swap_during_download = ("e4", v1)
    expire_recheck()
    db.get_connection()
    check("a download the object changed under fails",
          wait_for(lambda: db.status()["refresh"]["state"] == "failed"))
    check("and says why", "changed during the download" in (db.status()["refresh"]["error"] or ""),
          repr(db.status()["refresh"]))
    check("the site keeps serving the last good snapshot", title(db.get_connection()) == "v2")

    # The new ETag (e4) is a different object, so it is tried at once.
    expire_recheck()
    db.get_connection()
    check("a newer ETag is tried without waiting out the backoff",
          wait_for(lambda: db.status()["refresh"]["state"] == "ready"))
    check("and served", title(db.get_connection()) == "v1")

    # A short download: refused, backed off, retried later.
    reset(fake)
    fake.publish("e1", v1)
    db.get_connection()
    fake.publish("e2", v2)
    fake.short_write = True
    expire_recheck()
    db.get_connection()
    check("a truncated download is refused", wait_for(lambda: db.status()["refresh"]["state"] == "failed"))
    check("with the size in the error", "bytes" in (db.status()["refresh"]["error"] or ""))
    n = fake.downloads
    fake.short_write = False
    for _ in range(5):
        expire_recheck()
        db.get_connection()
    check("the same ETag is not retried on every request", fake.downloads == n, f"{fake.downloads} vs {n}")
    db._refresh["retry_at"] = 0
    expire_recheck()
    db.get_connection()
    check("and is retried after the backoff", wait_for(lambda: db.status()["refresh"]["state"] == "ready"))
    check("which then serves", title(db.get_connection()) == "v2")

    # Not a snapshot: missing tables.
    reset(fake)
    fake.publish("e1", v1)
    db.get_connection()
    broken = make_snapshot("broken", tables=("jobs",))
    fake.publish("e9", broken)
    expire_recheck()
    db.get_connection()
    check("a file without the API's tables is refused",
          wait_for(lambda: db.status()["refresh"]["state"] == "failed")
          and "missing tables" in (db.status()["refresh"]["error"] or ""))
    check("and leaves nothing behind in /tmp",
          sorted(p.name for p in Path(db.TMP_DIR).iterdir()) == [Path(db._path).name],
          repr(sorted(p.name for p in Path(db.TMP_DIR).iterdir())))

    # Far smaller than the current snapshot: refused before downloading.
    reset(fake)
    big = make_snapshot("big", pad=200_000)
    fake.publish("e1", big)
    db.get_connection()
    fake.publish("e2", v2)
    n = fake.downloads
    expire_recheck()
    db.get_connection()
    check("a snapshot far smaller than the current one is refused",
          wait_for(lambda: db.status()["refresh"]["state"] == "failed")
          and "bytes against" in (db.status()["refresh"]["error"] or ""))
    check("without downloading it", fake.downloads == n)

    # A quiet container: the download has run past JOIN_AFTER_SECONDS, so
    # the next request waits for it (the thread runs while it waits).
    reset(FakeS3())
    fake = db._s3
    fake.publish("e1", v1)
    db.get_connection()
    fake.publish("e2", v2)
    fake.gate = threading.Event()
    expire_recheck()
    db.get_connection()
    db._refresh["started_mono"] -= db.JOIN_AFTER_SECONDS + 1
    threading.Timer(0.3, fake.gate.set).start()
    started = time.monotonic()
    conn = db.get_connection()
    waited = time.monotonic() - started
    check("an old download is joined by the next request, which gets the new snapshot",
          title(conn) == "v2" and 0.2 < waited < db.JOIN_SECONDS, f"{title(conn)} after {waited:.2f}s")
    check("progress is reported", db.status()["refresh"]["bytes"] > 0 or db.status()["refresh"]["state"] == "idle")

    # Only one joining request a minute.
    fake.publish("e3", v3)
    fake.gate = threading.Event()
    expire_recheck()
    db.get_connection()
    db._refresh["started_mono"] -= db.JOIN_AFTER_SECONDS + 1
    db._refresh["last_join"] = time.monotonic()
    old_join = db.JOIN_SECONDS
    db.JOIN_SECONDS = 2
    started = time.monotonic()
    db.get_connection()
    check("a second join inside the minute does not wait", time.monotonic() - started < 1.5)
    db._refresh["last_join"] = 0.0
    started = time.monotonic()
    db.get_connection()
    check("a join waits at most JOIN_SECONDS", 1.0 < time.monotonic() - started < 8,
          f"{time.monotonic() - started:.2f}s")
    db.JOIN_SECONDS = old_join

    # Hung: abandoned, started again, and the late finish is discarded.
    hung_gate = fake.gate
    db._refresh["started_mono"] -= db.HUNG_AFTER_SECONDS + 1
    db._refresh["last_join"] = time.monotonic()   # keep this request from waiting
    fake.gate = None
    n = fake.downloads
    db.get_connection()
    check("a hung download is abandoned and started again", fake.downloads == n + 1,
          f"{fake.downloads} vs {n}")
    check("the new attempt finishes", wait_for(lambda: db.status()["refresh"]["state"] == "ready"))
    check("and is served", title(db.get_connection()) == "v3")
    before = sorted(p.name for p in Path(db.TMP_DIR).iterdir())
    hung_gate.set()
    time.sleep(0.5)
    after = sorted(p.name for p in Path(db.TMP_DIR).iterdir())
    check("the abandoned download's late finish is discarded",
          title(db.get_connection()) == "v3" and len(after) <= len(before), f"{before} -> {after}")

    # S3 unreachable on the recheck: keep serving.
    fake.head_object = lambda **kw: (_ for _ in ()).throw(RuntimeError("no network"))
    expire_recheck()
    check("an S3 error on the recheck keeps serving", title(db.get_connection()) == "v3")
finally:
    reset(FakeS3())
    shutil.rmtree(WORK, ignore_errors=True)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
