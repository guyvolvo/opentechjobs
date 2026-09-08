"""EventBridge-triggered Lambda for the once-daily jobs.db maintenance
pass -- currently just VACUUM, split out of the frequent re-poll cycle.

Reported live (2026-09-08): VACUUM rewrites the WHOLE database file
regardless of how many rows a given run actually touched, so running it
on every ~5-minute shard cycle (see scrape_handler.py) meant that cost
scaled with jobs.db's TOTAL size, not with the shard's own small slice
of work -- the same "cost grows forever with company count" problem
sharding exists to remove, just moved into the loader step instead of
the probe step. VACUUM doesn't need to run anywhere near that often:
it's reclaiming space from closed/updated rows, not a correctness
requirement for reads or writes in between.

Reuses load_to_sqlite.py's own tested pull-modify-push cycle rather than
writing a second VACUUM implementation: passing it an EMPTY resolved.json
(no rows to upsert) still gets the free parts of that cycle for free --
export_known(), update_meta() (including the timestamp-clustering
canary), and the conditional S3 push -- with VACUUM running by default
since this is the one caller that does NOT pass --skip-vacuum.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
TMP = Path("/tmp")
BUCKET = os.environ["DATA_BUCKET"]


def lambda_handler(event, context):
    empty_resolved = TMP / "resolved-empty.json"
    empty_resolved.write_text("[]", encoding="utf-8")

    # Generous timeout/no --skip-vacuum here on purpose: this is the one
    # place VACUUM is allowed to take real time against the full DB size,
    # and it only runs once a day, so being generous costs almost nothing
    # (see infra/variables.tf's scrape_maintenance_* variables for the
    # actual GB-second math).
    load = subprocess.run(
        [sys.executable, str(ROOT / "loader" / "load_to_sqlite.py"),
         "--resolved", str(empty_resolved), "--out", str(TMP / "jobs.db"),
         "--bucket", BUCKET, "--key", "jobs.db"],
        capture_output=True, text=True, timeout=180,
    )
    if load.stderr:
        print(load.stderr)
    if load.returncode != 0:
        raise RuntimeError(f"load_to_sqlite.py exited {load.returncode}")

    print("daily maintenance (VACUUM) complete")
    return {"ok": True}
