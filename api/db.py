"""
jobs.db lifecycle for the Lambda execution environment.

Cold start: download the snapshot from S3 into /tmp and open it
read-only. A container has nothing to serve until that finishes, so this
one download still happens inside the first request.

Warm: serve what is already open, and refresh in the background. At most
once a minute a request HEADs the S3 object. When the ETag has moved, a
background thread downloads the new snapshot to its own file, checks it,
and hands it over; the request that noticed, and every request while the
download runs, is answered from the previous snapshot. The swap happens
at the start of a later request, never under one.

Why. The snapshot is about 1GB and takes 12 to 15 seconds to fetch. The
refresh used to run inside whichever request noticed the new ETag, so
every container stalled a visitor for that long after each five-minute
merge. While it stalled, the browser's other calls could not use it,
Lambda started more containers, each of those downloaded the snapshot
too, and on 2026-09-17 the account's limit of 10 concurrent executions
turned the rest away as 503s. Reported live from a filter change right
after an update.

What a refresh guarantees:
  - one at a time per container; a new ETag seen while one runs waits
  - the download goes to its own file, never over the open one
  - the ETag is read before and after; if the object changed during the
    download the parts may be from two versions, so it is thrown away
  - the size must match S3's and be plausible next to the current file
  - the file must open, have the tables the API reads, and answer a query
  - a failure keeps the old snapshot, is logged, and is retried with
    backoff, not on every request
  - the old connection is closed only once no request can still be using
    it (a request can call get_connection() more than once)

A frozen container: Lambda pauses the process between invocations, so a
background download only runs while a request is being handled. On a
busy container that is most of the time and the download finishes in the
background. On a quiet one it barely moves: seen in production on the
first deploy, short requests seconds apart left a download at the same
place for minutes while the container kept serving the old snapshot.
So a download older than JOIN_AFTER_SECONDS is joined by the next
request, for up to JOIN_SECONDS: that request waits, the thread runs, and
staleness is bounded. Under real load the download has long finished by
then, which is the case the background refresh exists for.

Several containers each download their own copy. That is S3 to Lambda in
the same region, which costs no transfer, and each copy has to live on
that container's own /tmp anyway.
"""

import glob
import os
import sqlite3
import threading
import time

import boto3
from boto3.s3.transfer import TransferConfig

from job_filters import register_functions

DATA_BUCKET = os.environ["DATA_BUCKET"]
DATA_KEY = os.environ["DATA_KEY"]
TMP_DIR = "/tmp"
LOCAL_PREFIX = "jobs"
# Re-check S3 for a newer version at most this often, per warm container.
# Used to be 300s, exactly matching scrape-fast's own 5-min cadence --
# meaning a warm container's worst-case staleness was a FULL cycle, not
# a fraction of one. Reported live: a job fresh enough to trigger an
# alert email didn't show up at the top of the board yet (a different
# container, still serving its previous ETag check), then did a few
# minutes later once that container's own recheck finally fired -- not
# a sort bug, this exact per-container lag. 60s bounds that to at most
# one fast-poll cycle's worth of staleness instead of up to five, at the
# cost of 5x more HEAD requests -- cheap (no data transfer; the actual
# re-download only happens when the ETag has genuinely changed).
S3_RECHECK_SECONDS = 60
# A retired connection stays open this long. The API Gateway integration
# times out at 29s, so no request can still hold it after that.
RETIRE_AFTER_SECONDS = 40
# A download running longer than this is joined by the next request, for up
# to JOIN_SECONDS, so a quiet container still finishes it. 20s leaves room
# under the 29s API Gateway timeout for the request's own query.
JOIN_AFTER_SECONDS = 60
JOIN_SECONDS = 20
# At most one joining request a minute, so a download that is stuck does
# not make every request wait.
JOIN_EVERY_SECONDS = 60
# A download still running after this is abandoned and started again. Its
# thread cannot be stopped; if it ever finishes, its generation is stale
# and its file is thrown away.
HUNG_AFTER_SECONDS = 300
# A failed refresh of the same ETag is retried after this, doubling up to
# the cap.
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 900
# A new snapshot smaller than this share of the current one is refused.
# The file grows by a few MB a day; a half-sized one is a bad object.
MIN_SIZE_RATIO = 0.5
# Tables the API reads. A file without them is not a snapshot.
REQUIRED_TABLES = ("jobs", "companies", "meta")

_s3 = boto3.client("s3")

# download_file defaults to 10 concurrent 8MB parts, each buffered in
# memory. On a snapshot this size that is the largest single thing this
# function allocates, and it is why Max Memory Used sits near the
# configured ceiling on any invocation that refreshes. Two parts is still
# plenty of throughput inside a region and costs a fraction of the
# footprint, which is what lets the memory setting come down.
_TRANSFER = TransferConfig(max_concurrency=2, multipart_chunksize=8 * 1024 * 1024)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_path: str | None = None
_etag: str | None = None
_loaded_at: float | None = None
_last_checked: float = 0.0
_downloads = 0
# (connection, path, retired_at) waiting to be closed and deleted
_retired: list[tuple[sqlite3.Connection, str, float]] = []
# The background refresh. Guarded by _lock.
_refresh = {
    "state": "idle",          # idle | downloading | ready | failed
    "etag": None,             # the ETag being fetched, or last fetched
    "started_at": None,
    "finished_at": None,
    "seconds": None,
    "error": None,
    "failures": 0,
    "retry_at": 0.0,
    "ready": None,            # (conn, path, etag) once a download checks out
    "bytes": 0,               # downloaded so far
    "size": None,
    "started_mono": None,
    "thread": None,
    "gen": 0,                 # which download is current
    "last_join": 0.0,
}


def _local_path(etag: str) -> str:
    # Numbered as well as named by ETag, so a download can never land on
    # the path of the file being served, even for an ETag seen before.
    global _downloads
    _downloads += 1
    return os.path.join(TMP_DIR, f"{LOCAL_PREFIX}-{etag.strip(chr(34))}-{_downloads}.db")


def _head() -> tuple[str, int]:
    h = _s3.head_object(Bucket=DATA_BUCKET, Key=DATA_KEY)
    return h["ETag"], h["ContentLength"]


def _open_readonly(path: str) -> sqlite3.Connection:
    # uri=True + mode=ro: Lambda never writes to this file, and being
    # explicit about that is cheap insurance against a bug ever trying to.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    register_functions(conn)
    return conn


def _check(conn: sqlite3.Connection) -> None:
    """Raises unless this looks like a snapshot the API can serve."""
    names = {r[0] for r in conn.execute(
        f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
        f"({','.join('?' * len(REQUIRED_TABLES))})", REQUIRED_TABLES)}
    missing = set(REQUIRED_TABLES) - names
    if missing:
        raise ValueError(f"snapshot is missing tables: {sorted(missing)}")
    if conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() is None:
        raise ValueError("snapshot has no jobs")


def _progress(n: int) -> None:
    # Called from the transfer's worker threads. A plain add under the
    # lock; it only feeds status().
    with _lock:
        _refresh["bytes"] += n


def _fetch(etag: str, size: int, current_size: int | None) -> tuple[sqlite3.Connection, str]:
    """Download, check and open one snapshot. Returns (conn, path) or raises.
    Never touches the file that is being served."""
    if current_size and size < current_size * MIN_SIZE_RATIO:
        raise ValueError(f"new snapshot is {size} bytes against {current_size} now")
    final = _local_path(etag)
    tmp = final + ".part"
    try:
        _s3.download_file(DATA_BUCKET, DATA_KEY, tmp, Config=_TRANSFER, Callback=_progress)
        after, _ = _head()
        if after != etag:
            raise ValueError(f"object changed during the download ({etag} -> {after})")
        got = os.path.getsize(tmp)
        if got != size:
            raise ValueError(f"downloaded {got} bytes, S3 says {size}")
        os.replace(tmp, final)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    conn = _open_readonly(final)
    try:
        _check(conn)
    except Exception:
        conn.close()
        os.remove(final)
        raise
    return conn, final


def _background(gen: int, etag: str, size: int, current_size: int | None) -> None:
    started = time.monotonic()
    print(f"jobs.db refresh started: {_etag} -> {etag} ({size} bytes)")
    try:
        conn, path = _fetch(etag, size, current_size)
    except Exception as e:
        with _lock:
            if gen != _refresh["gen"]:
                print(f"jobs.db abandoned refresh of {etag} ended: {e!r}")
                return
            _refresh["failures"] += 1
            delay = min(RETRY_BASE_SECONDS * 2 ** (_refresh["failures"] - 1), RETRY_MAX_SECONDS)
            _refresh.update(state="failed", error=repr(e), retry_at=time.monotonic() + delay,
                            finished_at=time.time(), seconds=round(time.monotonic() - started, 1))
        # A failed refresh is real signal, not a hiccup to swallow quietly:
        # the site keeps serving the previous snapshot, and without this
        # line nothing would say why it stopped moving.
        print(f"jobs.db refresh failed, staying on {_etag}, retry in {delay}s: {e!r}")
        return
    seconds = round(time.monotonic() - started, 1)
    with _lock:
        if gen != _refresh["gen"]:
            conn.close()
            os.remove(path)
            print(f"jobs.db abandoned refresh of {etag} finished late, discarded")
            return
        _refresh.update(state="ready", ready=(conn, path, etag), error=None, failures=0,
                        finished_at=time.time(), seconds=seconds)
    print(f"jobs.db refresh ready: {etag} in {seconds}s")


def _start_refresh(etag: str, size: int) -> None:
    """Start a background download of etag unless one is already running,
    ready, or backing off for this same ETag. Caller holds _lock."""
    state = _refresh["state"]
    if state in ("downloading", "ready"):
        return
    if state == "failed" and _refresh["etag"] == etag and time.monotonic() < _refresh["retry_at"]:
        return
    if _refresh["etag"] != etag:
        _refresh["failures"] = 0
    current_size = os.path.getsize(_path) if _path and os.path.exists(_path) else None
    _refresh["gen"] += 1
    thread = threading.Thread(target=_background, args=(_refresh["gen"], etag, size, current_size),
                              name="jobs-db-refresh", daemon=True)
    _refresh.update(state="downloading", etag=etag, started_at=time.time(),
                    started_mono=time.monotonic(), bytes=0, size=size, thread=thread,
                    finished_at=None, seconds=None, error=None)
    thread.start()


def _close_retired(now: float) -> None:
    keep = []
    for conn, path, retired_at in _retired:
        if now - retired_at < RETIRE_AFTER_SECONDS:
            keep.append((conn, path, retired_at))
            continue
        try:
            conn.close()
        finally:
            if path and os.path.exists(path):
                os.remove(path)
    _retired[:] = keep


def _clear_leftovers() -> None:
    """Files a previous container life left in /tmp: half downloads, and
    snapshots nothing has open. /tmp persists across warm invocations and
    is only 3GB, two snapshots' worth."""
    for p in glob.glob(os.path.join(TMP_DIR, f"{LOCAL_PREFIX}-*.db*")) + [os.path.join(TMP_DIR, "jobs.db")]:
        if p != _path and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def get_connection() -> sqlite3.Connection:
    global _conn, _path, _etag, _loaded_at, _last_checked

    now = time.monotonic()
    if _conn is None:
        # Cold start: nothing to serve until this finishes. Not under the
        # lock, which the download's progress callback takes from the
        # transfer's own threads. Lambda runs one request per container,
        # so nothing else is here to race it.
        _clear_leftovers()
        etag, size = _head()
        conn, path = _fetch(etag, size, None)
        with _lock:
            _conn, _path, _etag, _loaded_at, _last_checked = conn, path, etag, time.time(), now
            return _conn

    with _lock:
        _close_retired(now)

        thread = _refresh["thread"]
        running = _refresh["state"] == "downloading" and thread is not None
        age = now - _refresh["started_mono"] if running else 0
        if running and age > HUNG_AFTER_SECONDS:
            print(f"jobs.db refresh of {_refresh['etag']} hung after {age:.0f}s "
                  f"({_refresh['bytes']} of {_refresh['size']} bytes), starting over")
            _refresh.update(state="idle", thread=None)
            _refresh["gen"] += 1
            _last_checked = 0.0   # check S3 again on this request
            running = False
        join = (running and age > JOIN_AFTER_SECONDS
                and now - _refresh["last_join"] > JOIN_EVERY_SECONDS)
        if join:
            _refresh["last_join"] = now

    if join:
        # Outside the lock: the download's progress callback needs it.
        thread.join(JOIN_SECONDS)

    with _lock:
        ready = _refresh["ready"]
        if ready:
            new_conn, new_path, new_etag = ready
            _retired.append((_conn, _path, now))
            _conn, _path, _etag, _loaded_at = new_conn, new_path, new_etag, time.time()
            _refresh.update(state="idle", ready=None)
            print(f"jobs.db now serving {new_etag}")

        if now - _last_checked < S3_RECHECK_SECONDS:
            return _conn
        _last_checked = now

    try:
        etag, size = _head()
    except Exception:
        return _conn  # S3 hiccup: keep serving what we have rather than fail the request

    with _lock:
        if etag != _etag:
            _start_refresh(etag, size)
        return _conn


def status() -> dict:
    """What this container is serving and how its refresh is going, for
    /api/health."""
    with _lock:
        return {
            "etag": (_etag or "").strip('"') or None,
            "loaded_at": None if _loaded_at is None else
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_loaded_at)),
            "age_seconds": None if _loaded_at is None else round(time.time() - _loaded_at),
            "refresh": {
                "state": _refresh["state"],
                "etag": (_refresh["etag"] or "").strip('"') or None,
                "seconds": _refresh["seconds"],
                "bytes": _refresh["bytes"],
                "size": _refresh["size"],
                "error": _refresh["error"],
                "failures": _refresh["failures"],
            },
        }
