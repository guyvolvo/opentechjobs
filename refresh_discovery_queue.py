"""Keeps pending-discovery-candidates.json topped up on its own -- see
.github/workflows/discover-companies.yml, which runs this on a daily
cron.

Without this, discover_companies.py only ever produced one one-time
batch (640 candidates, 2026-09-08, run by hand). The queue would drain
to zero and discovery would silently stop for good -- the exact "needs
a manual run every so often" shape this replaces. Common Crawl only
publishes a new crawl snapshot roughly monthly, so daily is already
more often than fresh URLs can realistically appear; what daily buys
is catching a new snapshot promptly, not finding more inside the same one.

Runs discover_companies.py for every ATS it knows how to search, merges
freshly-verified candidates into the existing queue (deduped by
(ats, token) against both what's already queued and whatever
discover_companies.py's own load_already_tracked call already
excluded), then re-sorts the WHOLE queue by Israel relevance -- see
discover_companies.py's own docstring for why that sort exists. A
fresh, more Israel-relevant candidate found today can jump ahead of an
already-queued but Israel-irrelevant one from a previous run, not just
get appended to the back and wait its turn.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
QUEUE_PATH = ROOT / "pending-discovery-candidates.json"
ATS_TYPES = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters"]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    queue = json.loads(QUEUE_PATH.read_text(encoding="utf-8")) if QUEUE_PATH.exists() else []
    seen = {(c["ats"], c["token"]) for c in queue}
    added = 0

    for ats in ATS_TYPES:
        print(f"discovering {ats}...", file=sys.stderr)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "discover_companies.py"), "--ats", ats,
             "--max-pages", "5", "--verify-limit", "300", "--json"],
            capture_output=True, text=True, timeout=900,
        )
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        if proc.returncode != 0:
            print(f"discover_companies.py --ats {ats} exited {proc.returncode}, skipping this ats", file=sys.stderr)
            continue
        try:
            candidates = json.loads(proc.stdout)
        except json.JSONDecodeError:
            print(f"couldn't parse discover_companies.py output for {ats}, skipping", file=sys.stderr)
            continue
        for c in candidates:
            key = (c["ats"], c["token"])
            if key in seen:
                continue
            seen.add(key)
            queue.append({
                "ats": c["ats"],
                "token": c["token"],
                "domain": c["guessed_domain"],
                "job_count": c["job_count"],
                "israel_job_count": c.get("israel_job_count", 0),
            })
            added += 1
        print(f"  {ats}: {added} new so far", file=sys.stderr)

    queue.sort(key=lambda c: (-c.get("israel_job_count", 0), -c["job_count"]))
    QUEUE_PATH.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"added {added} new candidates; queue now {len(queue)} total, re-sorted by Israel relevance", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
