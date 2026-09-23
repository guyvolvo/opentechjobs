"""Mirror pending delta fragments to local disk, so the box cannot miss one.

The box runs beside the Lambda pipeline, not instead of it, which means
the Lambda still owns deletion: it applies a fragment and removes it,
usually within five minutes of the sweep writing it. The box's own apply
takes longer than that (a 90MB batch is minutes of upserts on two
vCPUs), and it only looked at S3 between applies, so every fragment the
Lambda cleared during one of those windows was one the box never saw.
Measured the morning after the first night: the box's snapshot trailed
the live one by 15,500 listings.

So looking and applying are split. This runs every 30 seconds, costs one
LIST when there is nothing new, and copies anything it has not already
taken into a local spool. shadow_apply.py then works from the spool,
which nothing else can delete out from under it.

None of this is needed after a cutover, when the box deletes fragments
itself and a missed one is impossible by construction. It exists for as
long as two appliers are reading the same queue.

    python box/fetch_fragments.py
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

from deltas import PREFIX  # noqa: E402

DB = Path(os.environ.get("DATA_PATH", "/var/lib/otj/jobs.db"))
BUCKET = os.environ["DATA_BUCKET"]
SPOOL = DB.parent / "deltas"
# Keys this box has already taken a copy of, whether or not that copy
# has been applied and deleted since. Without it, every fetch would
# re-download everything the spool has already handed to an apply.
SEEN = DB.parent / "fetched-fragments.json"
# Enough history that a key cannot be forgotten while it is still listed
# in S3. The Lambda clears fragments within minutes and the sweep writes
# a handful every five, so this is hours of margin.
REMEMBER = 20000
WORKERS = 8
# The spool is a queue, not an archive. Past this the box is so far
# behind that hoarding more would fill the disk before it caught up, and
# the right answer is to notice rather than to keep downloading.
MAX_SPOOL_BYTES = 6 * 1024 * 1024 * 1024


def _load_seen() -> list[str]:
    try:
        return json.loads(SEEN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _spool_bytes() -> int:
    return sum(p.stat().st_size for p in SPOOL.glob("*.json"))


def main() -> int:
    started = time.monotonic()
    SPOOL.mkdir(parents=True, exist_ok=True)
    seen = _load_seen()
    known = set(seen)

    s3 = boto3.client("s3")
    keys: list[str] = []
    token = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".json")]
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated"):
            break

    wanted = sorted(k for k in keys if k not in known)
    if not wanted:
        print(f"nothing new ({len(keys)} in s3, spool {_spool_bytes() / 1048576:.0f}MB)")
        return 0

    room = MAX_SPOOL_BYTES - _spool_bytes()
    if room <= 0:
        print(f"spool is full at {_spool_bytes() / 1048576:.0f}MB, {len(wanted)} fragments "
              f"left in s3 this round", file=sys.stderr)
        return 0

    def fetch(key: str) -> tuple[str, int, bool]:
        """(key, bytes written, whether to stop asking for it)."""
        name = key[len(PREFIX):]
        # Downloaded beside the spool and moved into it, so a fragment
        # only becomes visible to an apply once it is whole. A rename
        # within one filesystem is atomic; a half-written file in the
        # spool would be parsed as one.
        tmp = SPOOL / (name + ".part")
        try:
            s3.download_file(BUCKET, key, str(tmp))
            size = tmp.stat().st_size
            tmp.replace(SPOOL / name)
            return key, size, True
        except Exception as e:  # noqa: BLE001 -- a miss is retried next run
            tmp.unlink(missing_ok=True)
            # The Lambda applier deletes a fragment as soon as it has
            # applied it, which can land between the listing above and
            # this GET. Nothing to fetch and nothing to retry: the
            # fragment is applied and gone, and this box simply never
            # saw it. Remembered so the next run stops asking.
            #
            # That is the one hole left while two appliers share a
            # queue, and it is now a two-second window rather than the
            # length of an apply. It closes completely at cutover, when
            # the box is the only thing deleting.
            gone = "Not Found" in str(e) or "NoSuchKey" in str(e) or isinstance(e, FileNotFoundError)
            print(f"{'gone before fetch' if gone else 'could not fetch'} {key}: {e!r}",
                  file=sys.stderr)
            return key, 0, gone

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(fetch, wanted))

    got = [k for k, size, _ in results if size]
    took = sum(size for _, size, _ in results)
    missed = sum(1 for _, size, _ in results if not size)
    # What landed, plus what will never land. A download that failed for
    # any other reason is left out so the next run retries it.
    remember = [k for k, size, settled in results if size or settled]
    SEEN.write_text(json.dumps((seen + remember)[-REMEMBER:]), encoding="utf-8")
    print(f"fetched {len(got)} of {len(wanted)} new fragments ({took / 1048576:.0f}MB) "
          f"in {time.monotonic() - started:.1f}s, spool now {_spool_bytes() / 1048576:.0f}MB"
          + (f", {missed} missed" if missed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
