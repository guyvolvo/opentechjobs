"""Apply pending delta fragments to the box's own jobs.db, in place.

The Lambda applier (scrape_maintenance_handler.py) pulls the 700MB
snapshot from S3, applies a few fragments, pushes it back and deletes
the fragments. This does the middle step against a file that never
moves, and nothing else: it does not delete fragments, upload a
snapshot, or write status files, because while the box runs in the
shadow of the Lambda stack the Lambda still owns all of that.

What it does share is the loader. load_to_sqlite.py with --resolved and
--out and no --bucket is exactly "apply this to that file," and --box
adds the category column, canonical posted_at and the board's indexes.
Descriptions still go to S3 through the loader's own put_many, keyed
by job id, the same objects the Lambda writes, so the two appliers
cannot disagree about them.

Fragments already applied are remembered in a small state file, since
the Lambda deletes them on its own schedule and this must not replay
the same one every minute. Order is preserved: keys sort by time and
the loader is an upsert, so a fragment the Lambda has already removed
by the time this looks is simply one this never sees.

Runs from a systemd timer every minute. A run that finds nothing costs
one S3 listing.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

from deltas import list_fragments_sized, read_fragments  # noqa: E402
import precompute  # noqa: E402

DB = Path(os.environ.get("DATA_PATH", "/var/lib/otj/jobs.db"))
BUCKET = os.environ["DATA_BUCKET"]
STATE = DB.with_name("applied-fragments.json")
WORK = DB.with_name("delta-resolved.json")
# Bounded by bytes, like the Lambda, but without its memory ceiling to
# respect: the box parses into RAM too, and 2GB is the whole machine.
MAX_APPLY_BYTES = 96 * 1024 * 1024
REMEMBER = 5000


def _load_state() -> list[str]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def main() -> int:
    started = time.monotonic()
    applied = _load_state()
    seen = set(applied)
    pending = [(k, n) for k, n in list_fragments_sized(BUCKET) if k not in seen]
    if not pending:
        print("nothing pending")
        return 0
    take, total = [], 0
    for key, size in pending:
        if take and total + size > MAX_APPLY_BYTES:
            break
        take.append(key)
        total += size
    results = read_fragments(BUCKET, take)
    if not results:
        print(f"{len(take)} fragments present but none readable", file=sys.stderr)
        return 1
    with WORK.open("w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False)
    companies = len(results)
    results = None
    read_s = time.monotonic() - started

    logos = DB.with_name("company-logos.json")
    cmd = [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
           "--resolved", str(WORK), "--out", str(DB),
           "--drop-description", "--skip-vacuum", "--skip-known", "--box"]
    if logos.exists():
        cmd += ["--logos", str(logos)]
    load = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
    if load.stderr:
        print(load.stderr.strip())
    if load.returncode != 0:
        print(f"load_to_sqlite.py exited {load.returncode}", file=sys.stderr)
        return load.returncode

    applied = (applied + take)[-REMEMBER:]
    STATE.write_text(json.dumps(applied), encoding="utf-8")
    apply_s = time.monotonic() - started
    # The /stats and /facets answers, under this box's own prefix
    # (PRECOMPUTED_PREFIX, see precompute.py) so the Lambda's copies are
    # never touched while the two run side by side. Paced inside
    # publish: it looks at the artifact's age and leaves a fresh one.
    written = precompute.publish(BUCKET, DB, "")
    print(f"applied {companies} companies from {len(take)} of {len(pending)} pending fragments "
          f"({total / 1048576:.0f}MB): read {read_s:.1f}s, apply {apply_s:.1f}s, "
          f"precomputed {len(written)}, total {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
