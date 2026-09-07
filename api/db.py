"""
jobs.db lifecycle for the Lambda execution environment.

Cold start: download jobs.db from S3 into /tmp, open it read-only. Warm
invocations reuse the same connection (module-level globals persist
across invocations), periodically HEAD-checking S3 (cheap, no data
transfer) so a long-lived container doesn't serve an outdated copy
indefinitely.
"""

import os
import sqlite3
import time

import boto3

from job_filters import register_functions

DATA_BUCKET = os.environ["DATA_BUCKET"]
DATA_KEY = os.environ["DATA_KEY"]
LOCAL_PATH = "/tmp/jobs.db"
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
# ~100MB re-download only happens when the ETag has genuinely changed,
# which is still gated by scrape-fast's own 5-min cadence, not this).
S3_RECHECK_SECONDS = 60

_s3 = boto3.client("s3")
_conn: sqlite3.Connection | None = None
_etag: str | None = None
_last_checked: float = 0.0


def _download() -> str:
    _s3.download_file(DATA_BUCKET, DATA_KEY, LOCAL_PATH)
    return _s3.head_object(Bucket=DATA_BUCKET, Key=DATA_KEY)["ETag"]


def _open_readonly() -> sqlite3.Connection:
    # uri=True + mode=ro: Lambda never writes to this file, and being
    # explicit about that is cheap insurance against a bug ever trying to.
    conn = sqlite3.connect(f"file:{LOCAL_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    register_functions(conn)
    return conn


def get_connection() -> sqlite3.Connection:
    global _conn, _etag, _last_checked

    now = time.monotonic()

    if _conn is None:
        # cold start: no local copy yet, must download
        _etag = _download()
        _conn = _open_readonly()
        _last_checked = now
        return _conn

    if now - _last_checked < S3_RECHECK_SECONDS:
        return _conn  # warm and recently checked, reuse as-is

    _last_checked = now
    try:
        current_etag = _s3.head_object(Bucket=DATA_BUCKET, Key=DATA_KEY)["ETag"]
    except Exception:
        return _conn  # S3 hiccup: keep serving what we have rather than fail the request

    if current_etag != _etag:
        _conn.close()
        _etag = _download()
        _conn = _open_readonly()

    return _conn
