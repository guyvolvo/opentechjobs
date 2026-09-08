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

Per-ats limits below reflect each platform's REAL available pool in
Common Crawl, measured live (2026-09-08), not a flat guess:
greenhouse ~1,784 unique tokens, ashby ~2,758, workable ~1,802,
smartrecruiters ~585 -- all previously left almost entirely unchecked
at the old flat 300/ats limit. Since verify_candidate() already scores
Israel relevance on every candidate it checks (no extra cost), simply
checking a much larger slice of an already-free, already-automated
source finds more real Israel-relevant companies without needing a
paid search API at all.

lever excluded entirely: confirmed live via jobs.lever.co/robots.txt --
`User-agent: CCBot / Disallow: /` blocks Common Crawl's own crawler
outright, so its index has essentially nothing for this host (62 URLs
across 10 pages, all robots.txt itself, zero real job-board captures).
Not a bug on this side to fix -- Lever's own robots.txt opts out of
Common Crawl specifically. The only way to find new Lever-hosted
companies is a real search engine's own index (a manual web search
found several live 2026-09-08), which this pipeline doesn't have
automated access to.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
QUEUE_PATH = ROOT / "pending-discovery-candidates.json"

# (max_pages, verify_limit) per ats -- see this module's own docstring
# for where these numbers come from.
ATS_LIMITS = {
    "greenhouse": (30, 1800),
    "ashby": (30, 2800),
    "workable": (20, 1800),
    "smartrecruiters": (20, 600),
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    queue = json.loads(QUEUE_PATH.read_text(encoding="utf-8")) if QUEUE_PATH.exists() else []
    seen = {(c["ats"], c["token"]) for c in queue}
    added = 0

    for ats, (max_pages, verify_limit) in ATS_LIMITS.items():
        print(f"discovering {ats} (max-pages={max_pages}, verify-limit={verify_limit})...", file=sys.stderr)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "discover_companies.py"), "--ats", ats,
             "--max-pages", str(max_pages), "--verify-limit", str(verify_limit), "--json"],
            capture_output=True, text=True, timeout=3600,
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
