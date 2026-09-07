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
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from probe import EMBED_ATS_PATTERNS, FETCHERS, KNOWN_FALSE_POSITIVES, UA

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
    return {
        "ats": ats,
        "token": token,
        "job_count": len(jobs),
        "guessed_domain": guessed_domain,
        "domain_verified": domain_verified,
        "sample_titles": [j.title for j in jobs[:3]],
    }


def main() -> int:
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

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    results = []
    to_check = new_tokens[: args.verify_limit]
    for i, token in enumerate(to_check):
        if i and i % 20 == 0:
            print(f"  verified {i}/{len(to_check)}...", file=sys.stderr)
        r = verify_candidate(sess, args.ats, token)
        if r:
            results.append(r)
        time.sleep(0.1)

    print(f"{len(results)} verified real boards with open jobs, out of {len(to_check)} checked", file=sys.stderr)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for r in sorted(results, key=lambda r: -r["job_count"]):
            mark = "OK" if r["domain_verified"] else "? "
            print(f"  [{mark}] {r['ats']}:{r['token']:<30} {r['job_count']:>4} jobs  -> {r['guessed_domain']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
