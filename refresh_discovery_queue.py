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

lever was excluded here until 2026-09-22, on the grounds that
jobs.lever.co/robots.txt blocked Common Crawl's crawler by name
(`User-agent: CCBot / Disallow: /`), leaving its index with 62 URLs for
the host and none of them a job board. Refetched today, that file is
three lines: `User-agent: * / Allow: / / Crawl-delay: 1`. The block is
gone. Common Crawl's index still has nothing, because an index reflects
crawls made while the block was live, so this is worth re-checking on
each new snapshot rather than assuming either way.

Meanwhile the Internet Archive indexes the host and always did: 2,535
distinct tokens off 750,000 rows in one sweep, still climbing when the
archive started throttling. That is what --source wayback is for.
"""

import argparse
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
    # A domain match over myworkdayjobs.com, now across ten crawl
    # snapshots rather than three (see CC_INDEXES_WORKDAY in
    # discover_companies.py for the measurements behind that).
    #
    # 8 pages, not 5. Five held one snapshot when this only read the
    # newest three, and CDX answers 400 past the last page, so a
    # generous limit costs one wasted request per snapshot rather than
    # three more. The older snapshots page differently and 5 would clip
    # them silently, which is the shape of missing data nobody notices.
    #
    # 3000, not 2000: the deeper sweep found 495 tenants the list did not
    # have, and a verify limit below the pool size drops the overflow
    # without saying so. Same lesson workable learned below.
    "workday": (8, 3000),
    # 4500, not 1800: reading three crawl snapshots instead of one took
    # workable's new-candidate count from a few hundred to 4,146 in the
    # first real run (2026-09-16), and a verify_limit of 1800 silently
    # dropped 2,346 of them. The cap is there to bound a run's wall
    # clock, not to sample the pool, so it has to move when the pool
    # does. Workable is the widest of these by some way: it produced
    # 4,604 unique slugs against greenhouse's 2,483.
    "workable": (20, 4500),
    "smartrecruiters": (20, 600),
    # Comeet's pool is small and it does not page: every prefix of
    # comeet.com/jobs/* answers with the same ~630 records, which come to
    # roughly 170 companies in one crawl and about 330 across several. So
    # max_pages buys nothing here and verify_limit is set to cover the
    # whole pool rather than a slice of it.
    #
    # Each candidate costs two fetches instead of one, the board page for
    # the token and then the API to confirm, which is what a non-guessable
    # ATS costs. Worth it: measured 2026-09-16, 219 companies carrying 963
    # Israeli roles, 943 of them on no other board here.
    "comeet": (1, 400),
    # Subdomain-shaped, reached through matchType=domain (see
    # CC_URL_PATTERNS).
    #
    # These pools are one to two orders of magnitude larger than the first
    # estimate. That estimate came from a hand-run curl capped at 3,000
    # records, so it was a floor and read as a total: roughly 230, 213 and
    # 192 companies. Measured through this code path instead, with no cap:
    # recruitee 833 untracked, breezy 2,326, jazzhr 1,181.
    #
    # Those are floors too. Repeat runs returned 1,577 and 905, because a
    # CDX 502 used to truncate a snapshot without retrying (fixed in
    # fetch_cc_urls), and any run that lost a snapshot undercounts. Sized
    # to the largest observed with headroom rather than to an average: the
    # cap is here to bound wall clock, and undershooting it drops
    # candidates in silence, which is what it did to workable.
    #
    # These grow the global board and barely touch Israel: sampling 40 of
    # each found 2, 0 and 0 Israeli roles.
    "recruitee": (1, 1600),
    "jazzhr": (1, 2500),
    "breezy": (1, 3500),
    # Read from the Wayback index instead, because Common Crawl has
    # nothing for this host: four snapshots returned zero URLs where
    # Wayback returned 115,531 covering 1,404 tenants. 1,600 covers the
    # whole pool with headroom, for the same reason recruitee's does.
    # Measured 2026-09-22: 676 of those tenants answer with open roles
    # and 19,523 postings, and 670 of the 676 hand over the employer's
    # real domain rather than a guess.
    "pinpoint": (1, 1600),
    # Pools measured 2026-09-22, sized above them so the cap bounds wall
    # clock rather than sampling the pool. Sampling 80 tokens of each
    # through the repo's own fetchers: bamboohr 4.96 jobs per sampled
    # token, teamtailor 8.25, personio 6.39. These are SMB platforms and
    # the mean tenant has five to eight roles open, so the three together
    # are worth roughly 47,000 jobs, not the hundreds of thousands the
    # raw tenant counts suggest.
    "bamboohr": (1, 6000),
    "personio": (1, 1800),
    "teamtailor": (1, 1600),
    # Lever through the Wayback index. 2,535 distinct tokens off 750,000
    # rows before the archive started throttling, and the count was still
    # climbing steeply at the cut, so the real pool is larger and this cap
    # is set for it. 11.66 jobs per sampled token, the highest of any pool
    # measured, because Lever skews to larger employers.
    "lever": (1, 5000),
}

# Which index an ATS is discovered through. Common Crawl unless named
# here. Worth knowing for later: Lever is excluded from ATS_LIMITS above
# because jobs.lever.co/robots.txt blocks Common Crawl's crawler
# outright, and that argument says nothing about this index.
ATS_SOURCE = {"pinpoint": "wayback", "lever": "wayback"}


def _discover(args: list[str], label: str) -> list[dict]:
    """One discover_companies.py run, as candidates. An empty list on any
    failure: one ATS misbehaving is not a reason to lose the rest."""
    proc = subprocess.run([sys.executable, str(ROOT / "discover_companies.py"), *args, "--json"],
                          capture_output=True, text=True, timeout=3600)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        print(f"discover_companies.py {label} exited {proc.returncode}, skipping", file=sys.stderr)
        return []
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"couldn't parse discover_companies.py output for {label}, skipping", file=sys.stderr)
        return []


def directory_pass(queue: list, seen: set, location: str) -> int:
    """Workable's own cross-customer search, filtered by location: the one
    directory any of these ATSes publishes, and the only Israel-first
    source here that is not a crawl.

    Deliberately its own weekly pass rather than part of the daily Common
    Crawl one. It is cheap (one search plus a verify per candidate) and it
    moves slowly: measured 2026-09-18, 21 candidates not already tracked,
    13 with live boards, 2,907 jobs between them but only 42 Israeli, one
    recruiting account accounting for 2,560 of the jobs and 2 of the
    Israeli ones. So the number to read run over run is new Israeli jobs
    and new domains, never total jobs, which one such account can carry on
    its own.
    """
    candidates = _discover(["--ats", "workable", "--source", "directory", "--location", location],
                           f"--source directory --location {location}")
    known_domains = {l.strip().lower() for l in (ROOT / "domains.txt").read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")}
    stats = {"location": location, "candidates": len(candidates), "live_boards": 0, "open_boards": 0,
             "israeli_boards": 0, "queued_new": 0, "already_queued": 0, "new_domains": 0,
             "jobs": 0, "israeli_jobs": 0, "empty_boards": 0}
    for c in candidates:
        jobs, il = c.get("job_count", 0), c.get("israel_job_count", 0)
        stats["live_boards"] += 1
        stats["jobs"] += jobs
        stats["israeli_jobs"] += il
        stats["open_boards"] += 1 if jobs else 0
        stats["empty_boards"] += 0 if jobs else 1
        stats["israeli_boards"] += 1 if il else 0
        domain = (c.get("guessed_domain") or "").lower()
        if domain and domain not in known_domains:
            stats["new_domains"] += 1
        key = (c["ats"], c["token"])
        if key in seen:
            stats["already_queued"] += 1
            continue
        seen.add(key)
        queue.append({"ats": c["ats"], "token": c["token"], "domain": c.get("guessed_domain"),
                      "domain_verified": bool(c.get("domain_verified")),
                      "job_count": jobs, "israel_job_count": il})
        stats["queued_new"] += 1
    queue.sort(key=lambda c: (-c.get("israel_job_count", 0), -c["job_count"]))
    QUEUE_PATH.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")
    # One line, parseable, so several runs can be compared without digging
    # through the log.
    print("directory-run " + json.dumps(stats, ensure_ascii=False))
    print(f"queued {stats['queued_new']} new candidates; queue now {len(queue)} total", file=sys.stderr)
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only-directory", action="store_true",
                     help="skip the Common Crawl pass and run only the ATS's own directory "
                          "search, filtered by --location. Workable is the only ATS that "
                          "publishes one. Cheap enough to run weekly on its own schedule.")
    ap.add_argument("--location", default="Israel", help="location filter for the directory pass")
    args = ap.parse_args()

    queue = json.loads(QUEUE_PATH.read_text(encoding="utf-8")) if QUEUE_PATH.exists() else []
    seen = {(c["ats"], c["token"]) for c in queue}
    added = 0

    if args.only_directory:
        return directory_pass(queue, seen, args.location)

    for ats, (max_pages, verify_limit) in ATS_LIMITS.items():
        source = ATS_SOURCE.get(ats, "commoncrawl")
        print(f"discovering {ats} from {source} (max-pages={max_pages}, "
              f"verify-limit={verify_limit})...", file=sys.stderr)
        candidates = _discover(["--ats", ats, "--source", source, "--max-pages", str(max_pages),
                                "--verify-limit", str(verify_limit)], f"--ats {ats}")
        for c in candidates:
            key = (c["ats"], c["token"])
            if key in seen:
                continue
            seen.add(key)
            queue.append({
                "ats": c["ats"],
                "token": c["token"],
                "domain": c["guessed_domain"],
                # Carried through, not dropped. discover_companies.py
                # already does the work of checking whether its
                # {token}.com guess resolves to anything (see
                # _guess_domain), and this queue used to read
                # guessed_domain while ignoring the verdict sitting right
                # next to it. Every unverified guess then became a real
                # company's permanent identity: confirmed live, the
                # guesser returns False for both "headoutcareers" and
                # "informagroupplc", and the board still shows
                # headoutcareers.com as a company with 20 open jobs when
                # the company is Headout at headout.com. Roughly a fifth
                # of a 40-company sample was invented this way, one of
                # them literally named stealth-healthtech-startup.com.
                #
                # Kept rather than dropped, deliberately: these are real
                # companies with real listings reached through a real ATS
                # board, and the only wrong part is the hostname we
                # guessed for them. Discarding the candidate would throw
                # away genuine jobs to avoid a cosmetic error. Recording
                # the flag lets the loader and the UI stop presenting a
                # guess as fact.
                "domain_verified": bool(c.get("domain_verified")),
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
