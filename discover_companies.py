"""Common Crawl based company discovery.

Finds NEW company boards by searching Common Crawl's URL index for each
ATS's public board URL pattern (the exact same host patterns
EMBED_ATS_PATTERNS in probe.py already trusts for scraping ONE
company's careers page), instead of only ever finding companies someone
happened to hand-type into domains.txt. Verified live (2026-09-07):
Common Crawl's CDX API returns real, currently-live board URLs this
way -- a sample against boards.greenhouse.io/* found "abnormalsecurity"
among others, confirmed against the real API to be a genuine board
with 66 open jobs.

Doesn't filter by company location on acceptance. This project already
doesn't require that of any company it tracks (plenty of already-pinned
ones -- Snowflake, Elastic -- aren't Israel-exclusive either); Israel
relevance stays a filter on the BOARD (israel_only=1), same as today,
not a gate on which companies get added in the first place.

Does, however, RANK candidates by it. Reported live (2026-09-08): after
one night's worth of batches merged, the Israel-only view had barely
moved (1,174 -> still ~1,174) even though the DB's total open-job count
had grown by thousands -- Common Crawl finds real boards anywhere on
the open web, with no location signal in the URL pattern itself, so a
FIFO queue of "whatever Common Crawl happened to return" merges mostly
non-Israeli companies first purely by chance. verify_candidate() already
fetches each candidate's full job list to confirm it's real; counting
how many of those jobs carry an Israel-matching location (IL_KEYWORDS,
the exact list api/job_filters.py's israel_only filter already uses,
imported rather than duplicated so the two never drift apart) costs
nothing extra -- no new API calls, no location-targeted search step, no
manual work. Sorting the output by that count means every future batch
merge_discovered_batch.py pops off the front of the queue is
Israel-heaviest first, entirely inside the existing unattended cron --
not a parallel manual process to keep running by hand.

Never auto-merges into domains.txt -- outputs verified candidates for
review, same discipline as every hand-verified companies.yml pin in
this project. A guessed domain ({token}.com) resolving to a real
website isn't proof it's the SAME company the token belongs to, just a
plausible starting point worth a human glance before merging.

Usage:
    python discover_companies.py --ats greenhouse --json > candidates.json
    python discover_companies.py --ats lever --max-pages 5 --verify-limit 200
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "api"))
from probe import EMBED_ATS_PATTERNS, FETCHERS, KNOWN_FALSE_POSITIVES, UA
from job_filters import IL_KEYWORDS

CC_INDEX = "https://index.commoncrawl.org/CC-MAIN-2026-34-index"

# CDX's own wildcard syntax (a URL prefix, not arbitrary regex) --
# mirrors EMBED_ATS_PATTERNS' own hosts. Only the ATSes with a plain
# guessable-token URL shape; personio/recruitee's token sits in the
# subdomain, which CDX's own domain-level index handles differently
# (matchType=domain), not built here yet.
CC_URL_PATTERNS = {
    "greenhouse": "boards.greenhouse.io/*",
    "lever": "jobs.lever.co/*",
    "ashby": "jobs.ashbyhq.com/*",
    "workable": "apply.workable.com/*",
    "smartrecruiters": "jobs.smartrecruiters.com/*",
}

_TOKEN_PATTERNS = dict(EMBED_ATS_PATTERNS)

SITE_ORIGIN = "https://opentechjobs.org"


def fetch_cc_urls(url_pattern: str, max_pages: int) -> list[str]:
    """Pages through Common Crawl's CDX API for one URL pattern. Each
    page is a real HTTP request against Common Crawl's own index
    servers -- max_pages bounds this, not a hard API limit.
    """
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    urls = []
    for page in range(max_pages):
        try:
            resp = sess.get(CC_INDEX, params={"url": url_pattern, "output": "json", "page": page}, timeout=30)
        except requests.RequestException as e:
            print(f"    page {page}: request failed: {e!r}", file=sys.stderr)
            break
        if resp.status_code != 200 or not resp.text.strip():
            break
        for line in resp.text.strip().split("\n"):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = d.get("url")
            if u:
                urls.append(u)
    return urls


def extract_tokens(ats: str, urls: list[str]) -> set[str]:
    pattern = _TOKEN_PATTERNS[ats]
    tokens = set()
    for url in urls:
        m = pattern.search(url)
        if m:
            tokens.add(m.group(1).lower())
    return tokens


def load_already_tracked(ats: str) -> set[str]:
    """Every token this project already resolves for this ats, read
    from the live API (not domains.txt, which stores bare domains, not
    the tokens they resolve to) -- so discovery only surfaces genuinely
    NEW candidates, not ones already tracked under some other domain
    spelling.
    """
    try:
        resp = requests.get(f"{SITE_ORIGIN}/api/companies", params={"resolved_only": "1", "ats": ats}, timeout=15)
        resp.raise_for_status()
        return {c["token"].split(":")[0].lower() for c in resp.json()["companies"] if c.get("token")}
    except requests.RequestException as e:
        print(f"couldn't load already-tracked tokens from the live API ({e!r}) -- "
              f"proceeding without exclusion, results may include known companies", file=sys.stderr)
        return set()


def verify_candidate(sess: requests.Session, ats: str, token: str) -> dict | None:
    if (ats, token) in KNOWN_FALSE_POSITIVES:
        return None
    try:
        jobs = FETCHERS[ats](sess, token)
    except Exception:
        return None
    if not jobs:
        return None
    guessed_domain = f"{token}.com"
    domain_verified = False
    try:
        r = sess.head(f"https://{guessed_domain}", timeout=5, allow_redirects=True)
        domain_verified = r.status_code < 400
    except requests.RequestException:
        pass
    # Free: `jobs` is already the full list this call just fetched to
    # confirm the board is real. Counting Israel-matching locations here
    # costs nothing extra and is what lets main() rank the whole batch by
    # Israel relevance instead of merging in whatever order Common Crawl
    # happened to return.
    israel_job_count = sum(
        1 for j in jobs if j.location and any(kw in j.location.lower() for kw in IL_KEYWORDS)
    )
    return {
        "ats": ats,
        "token": token,
        "job_count": len(jobs),
        "israel_job_count": israel_job_count,
        "guessed_domain": guessed_domain,
        "domain_verified": domain_verified,
        "sample_titles": [j.title for j in jobs[:3]],
    }


def main() -> int:
    # Same reconfigure probe.py's own main() does -- a job title can
    # carry any real unicode (an em-dash, a curly quote, a non-Latin
    # name), and Windows' default console codec isn't UTF-8. Reported
    # live: a full 400-candidate run completed all its real work, then
    # crashed on the final print, losing every result to a
    # UnicodeEncodeError that had nothing to do with the data itself.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ats", required=True, choices=sorted(CC_URL_PATTERNS), help="which ATS to search")
    ap.add_argument("--max-pages", type=int, default=3, help="Common Crawl CDX pages to fetch")
    ap.add_argument("--verify-limit", type=int, default=100, help="cap on how many new candidates to live-verify")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print(f"querying Common Crawl for {CC_URL_PATTERNS[args.ats]} ...", file=sys.stderr)
    urls = fetch_cc_urls(CC_URL_PATTERNS[args.ats], args.max_pages)
    print(f"  {len(urls)} URLs found", file=sys.stderr)

    tokens = extract_tokens(args.ats, urls)
    print(f"  {len(tokens)} unique candidate slugs extracted", file=sys.stderr)

    known = load_already_tracked(args.ats)
    print(f"  {len(known)} already tracked for {args.ats}", file=sys.stderr)
    new_tokens = sorted(tokens - known)
    print(f"  {len(new_tokens)} genuinely new candidates to verify", file=sys.stderr)

    # I/O-bound (waiting on each candidate's own ATS API + a domain HEAD
    # check), same reasoning as probe.py's own WORKERS -- concurrency
    # buys real wall-clock time for free. A fresh Session per worker
    # (not one shared across threads issuing the domain-guess HEAD
    # requests) avoids connection-pool contention at this width.
    results = []
    to_check = new_tokens[: args.verify_limit]
    done = 0

    def _verify(token: str):
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA})
        return verify_candidate(sess, args.ats, token)

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(_verify, token): token for token in to_check}
        for fut in as_completed(futures):
            done += 1
            if done % 50 == 0:
                print(f"  verified {done}/{len(to_check)}...", file=sys.stderr)
            r = fut.result()
            if r:
                results.append(r)

    print(f"{len(results)} verified real boards with open jobs, out of {len(to_check)} checked", file=sys.stderr)
    # Israel-heaviest first -- see this module's own docstring for why:
    # this is the whole fix for the merge queue draining mostly-non-Israeli
    # companies first, and it's just a sort, not a new discovery step.
    results.sort(key=lambda r: (-r["israel_job_count"], -r["job_count"]))
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for r in results:
            mark = "OK" if r["domain_verified"] else "? "
            print(f"  [{mark}] {r['ats']}:{r['token']:<30} {r['job_count']:>4} jobs "
                  f"({r['israel_job_count']} IL)  -> {r['guessed_domain']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
