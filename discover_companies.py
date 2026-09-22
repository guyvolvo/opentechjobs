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

Workable is the one ATS with a better source than Common Crawl. It
publishes a search across every board it hosts, filterable by location,
and each result carries the employer's own website. --source directory
reads that instead: no guessed domains, and the Israel ranking happens
at the source. Measured 2026-09-11: 36 employers hiring in Israel, 32
new to this project, 24 verified as real boards. The eight misses are
accounts whose token is not their URL slug (Risco Group's board is
"risco", its slug is "risco-group"); nothing public maps one to the
other, so those still need the Common Crawl path. Lever, Ashby,
Greenhouse and SmartRecruiters publish no equivalent search, and
Comeet's needs a per-account token that only the company's own site
carries.

Usage:
    python discover_companies.py --ats greenhouse --json > candidates.json
    python discover_companies.py --ats lever --max-pages 5 --verify-limit 200
    python discover_companies.py --ats workable --source directory --json
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "api"))
from probe import EMBED_ATS_PATTERNS, FETCHERS, KNOWN_FALSE_POSITIVES, UA, workday_israel_count, workday_page1
from job_filters import IL_KEYWORDS

# Common Crawl publishes a snapshot roughly monthly, and a board only
# appears in the ones whose crawl happened to reach it. Reading a single
# index therefore sees a fraction of what is on offer, and which fraction
# is luck rather than recency.
#
# Measured 2026-09-16 against boards.greenhouse.io/*: the newest snapshot
# held 310 distinct tokens, the two before it held 2,512 between them,
# and the union carried 1,601 tokens this project had never resolved. The
# newest crawl was not a superset of the older ones or even close to one.
#
# Three, not more: each one costs a full pass of CDX requests per ATS,
# CDX is slow and answers 502 under load, and the gain flattens as the
# snapshots overlap.
CC_INDEXES = [
    "https://index.commoncrawl.org/CC-MAIN-2026-39-index",
    "https://index.commoncrawl.org/CC-MAIN-2026-34-index",
    "https://index.commoncrawl.org/CC-MAIN-2026-30-index",
    "https://index.commoncrawl.org/CC-MAIN-2026-25-index",
]

# Workday gets a deeper pass than the rest, and only Workday.
#
# The three-snapshot argument above is about diminishing returns, and it
# holds for an ATS whose token sits in the URL path: a prefix match finds
# essentially all of them in one snapshot. Workday is the opposite shape.
# The tenant is a subdomain, so this is a matchType=domain sweep, and
# each snapshot is a different sample of a very long tail.
#
# Measured 2026-09-21, against a list of 1,807 tenants, counting only
# tenants not already known:
#
#     CC-MAIN-2026-39    77 new
#     CC-MAIN-2026-21   145 new
#     CC-MAIN-2026-12   241 new
#     CC-MAIN-2026-08    75 new
#     CC-MAIN-2025-43    67 new
#
# 495 in total, a 27% larger list, and that is a floor: three more
# snapshots in the same run answered with a truncated response and
# contributed nothing. There is no sign of the curve flattening, which
# is what a long tail looks like from the inside.
#
# Workday is also where the payoff is. It is 186,625 of the board's
# 481,272 open roles, at a mean of 240 a tenant, so a tenant found here
# is worth several from anywhere else.
CC_INDEXES_WORKDAY = CC_INDEXES + [
    "https://index.commoncrawl.org/CC-MAIN-2026-21-index",
    "https://index.commoncrawl.org/CC-MAIN-2026-17-index",
    "https://index.commoncrawl.org/CC-MAIN-2026-12-index",
    "https://index.commoncrawl.org/CC-MAIN-2026-08-index",
    "https://index.commoncrawl.org/CC-MAIN-2025-51-index",
    "https://index.commoncrawl.org/CC-MAIN-2025-43-index",
]

# CDX's own wildcard syntax (a URL prefix, not arbitrary regex) --
# mirrors EMBED_ATS_PATTERNS' own hosts.
#
# Two shapes here. Most of these put the company's token in the path, so
# a prefix match finds them. Recruitee, Breezy and JazzHR put it in the
# subdomain instead, which a prefix cannot reach: CDX needs
# matchType=domain for those, which this file said was "not built here
# yet" until it was measured and turned out to work first time.
# CC_DOMAIN_MATCH below names them.
CC_URL_PATTERNS = {
    "greenhouse": "boards.greenhouse.io/*",
    "lever": "jobs.lever.co/*",
    "ashby": "jobs.ashbyhq.com/*",
    "workable": "apply.workable.com/*",
    "smartrecruiters": "jobs.smartrecruiters.com/*",
    # A domain match, like the three below: every tenant is its own host
    # ({tenant}.{wd}.myworkdayjobs.com) and the site slug is the first
    # path segment. Measured 2026-09-21: 1,609 distinct tenants in one
    # snapshot against 26 pinned by hand. Read by extract_tokens's own
    # workday branch into "tenant:wd:site", the token f_workday takes.
    "workday": "myworkdayjobs.com",
    # Subdomain-shaped, queried with matchType=domain. Measured
    # 2026-09-16 against CC-MAIN-2026-34, each capped at 3,000 records so
    # these are floors: recruitee 230 companies, jazzhr 213, breezy 192.
    # Sampling 40 of each found 34, 30 and 29 live boards carrying 925,
    # 451 and 507 open jobs. Almost none of it is Israeli (2, 0 and 0 in
    # those samples), so this grows the global board rather than the
    # Israeli one, which is the trade this was added knowing.
    #
    # Teamtailor is absent, but not for the reason first written here.
    # The claim was that its URLs are "shaped some other way" because a
    # domain match returns no {company}.teamtailor.com hosts. That part is
    # true and the conclusion drawn from it was wrong: Common Crawl
    # indexed teamtailor's marketing site, and the tenant name is sitting
    # in the powered-by referral links those career sites send back, as
    # utm_content=<tenant>.teamtailor.com. It is recoverable.
    #
    # The real reason to stay out is weaker and worth stating honestly.
    # In a 300-record sample only 14 tenants were teamtailor-hosted; the
    # other 284 ran on their own career domains (career.addsecure.com and
    # the like), which no token guess can reach. That sample was
    # alphabetically truncated, every tenant starting a or b, so the ratio
    # is not trustworthy either. Sizing the hosted pool with one uncapped
    # pull is the measurement this needs before it earns a place above.
    "recruitee": "recruitee.com",
    "breezy": "breezy.hr",
    "jazzhr": "applytojob.com",
    # Same subdomain shape, and the richest of the four. Measured
    # 2026-09-22 over the Wayback index rather than here: 1,404 tenants,
    # of which 676 answer with open roles and 19,519 postings between
    # them. The weight is US and UK, not Israel, which is a trade worth
    # making with eyes open -- 2 Israeli listings in the whole harvest.
    # Wayback found them because Common Crawl's coverage of this host is
    # thinner; if a run here comes back with far fewer than 1,404, that
    # is the reason and not a bug.
    "pinpoint": "pinpointhq.com",
    # The exception to the "guessable token" rule above, and the reason
    # it is worth making one. Comeet is what most Israeli startups
    # actually run, and no amount of token guessing reaches it: the API
    # wants an opaque per-company token that appears nowhere in the URL.
    # companies.yml's first Comeet entries were therefore extracted by
    # hand, and its own header calls a Common Crawl harvest the
    # alternative nobody had built. This is that harvest. The board URL
    # carries a slug and a uid, the token sits in the page they address,
    # so one extra fetch per candidate turns an index hit into a pin.
    "comeet": "comeet.com/jobs/*",
}

# Queried with matchType=domain rather than a URL prefix, because the
# company's token is the subdomain. CDX returns every URL under the host
# for these, so extract_tokens does the narrowing.
CC_DOMAIN_MATCH = frozenset({"recruitee", "breezy", "jazzhr", "workday", "pinpoint"})
WORKDAY_URL_RE = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Za-z]{2}/)?([A-Za-z0-9_-]+)(?:/|\?|$)", re.I)
# Path segments that are not a site: a job page's own prefix, the login
# page, and Workday's own API root.
_WORKDAY_NOT_A_SITE = frozenset({"job", "jobs", "login", "wday", "static", "assets"})

# Worth another go: CDX sheds load with these rather than saying anything
# about the query. Everything else non-200 is an answer, and the one that
# matters is the 400 returned for a page past the end, which is how
# fetch_cc_urls learns a snapshot is finished. Retrying that would turn
# every completed snapshot into three wasted requests and a warning.
CDX_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_TOKEN_PATTERNS = dict(EMBED_ATS_PATTERNS)
# EMBED_ATS_PATTERNS carries recruitee already (it is a real embed shape
# on a company's own careers page). Breezy and JazzHR are only ever seen
# here, in the crawl index, so they are spelled out rather than added to
# that list and implying probe.py scrapes for them.
#
# Anchored on the scheme so a path segment that merely mentions the host
# cannot masquerade as a subdomain, and the obvious non-company hosts are
# dropped: every tenant answers on www/api/static too.
_TOKEN_PATTERNS.setdefault("breezy", re.compile(r"https?://([a-zA-Z0-9-]+)\.breezy\.hr"))
_TOKEN_PATTERNS.setdefault("jazzhr", re.compile(r"https?://([a-zA-Z0-9-]+)\.applytojob\.com"))
_TOKEN_PATTERNS.setdefault("pinpoint", re.compile(r"https?://([a-zA-Z0-9-]+)\.pinpointhq\.com"))
_NON_TENANT_SUBDOMAINS = frozenset({"www", "api", "static", "assets", "cdn", "app", "jobs", "help",
                                    "support", "marketing-assets"})

# Both halves of a Comeet board URL: /jobs/{slug}/{uid}.
COMEET_JOB_RE = re.compile(r"comeet\.com/jobs/([A-Za-z0-9_.-]+)/([A-Za-z0-9.]+)")
# The token as the board page embeds it, either through Comeet's
# WordPress plugin or its generic widget. Same two shapes probe.py's own
# f_comeet_scrape already knows, read here off comeet.com rather than off
# the company's site, which is what makes this work for a company whose
# own careers page embeds nothing at all.
COMEET_TOKEN_RES = [
    re.compile(r'"token"\s*:\s*"([^"]{16,})"'),
    re.compile(r'comeet_token"?\s*[:=]\s*"([^"]{16,})"'),
]
COMEET_POSITIONS = "https://www.comeet.com/careers-api/1.0/company/{uid}/positions?token={token}"

SITE_ORIGIN = "https://opentechjobs.org"


def fetch_cc_urls(url_pattern: str, max_pages: int, indexes=None) -> list[str]:
    """Pages through Common Crawl's CDX API for one URL pattern, once per
    snapshot in CC_INDEXES. Each page is a real HTTP request against
    Common Crawl's own index servers, and max_pages bounds this per
    snapshot rather than being a hard API limit.

    A prefix runs out of pages long before max_pages does: CDX reported
    one or two for every ATS host here, and asking for a page past the
    end answers 400, which the break below reads as "this snapshot is
    done". So a generous max_pages costs one wasted request per snapshot,
    not thirty.

    Retried per page, because CDX answers 502 and 504 under load often
    enough to matter. Without it a transient failure on page 0 silently
    drops a whole snapshot's candidates and the run still reports
    success, which is the shape of missing data nobody notices.

    The first version of that retry only caught requests exceptions, so
    it never fired: a 502 is a perfectly good response object, and the
    status check below read it as "this snapshot is finished". Measured
    live, breezy came back with 2,326 tenants on one run and 1,577 on the
    next, a whole snapshot apart, both exiting 0. Retrying the status is
    the part that was missing, and telling the two kinds of non-200 apart
    is what makes it safe: a 400 means the page is past the end, which is
    how this loop learns to stop.
    """
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    # A bare host with no wildcard is one of the subdomain ATSes, and CDX
    # will not find those by prefix: "recruitee.com" as a URL prefix
    # matches nothing, because every real URL starts with the tenant.
    # matchType=domain asks for everything under the host instead, and
    # extract_tokens narrows it back down to tenants.
    domain_match = "*" not in url_pattern
    urls = []
    for index in (indexes or CC_INDEXES):
        snapshot = index.rsplit("/", 1)[-1]
        for page in range(max_pages):
            resp = None
            params = {"url": url_pattern, "output": "json", "page": page}
            if domain_match:
                params["matchType"] = "domain"
            for attempt in (1, 2, 3):
                try:
                    resp = sess.get(index, params=params, timeout=60)
                except requests.RequestException as e:
                    resp = None
                    if attempt == 3:
                        print(f"    {snapshot} page {page}: request failed after 3 tries: {e!r}",
                              file=sys.stderr)
                        break
                    time.sleep(2 * attempt)
                    continue
                if resp.status_code not in CDX_RETRYABLE_STATUS:
                    break
                if attempt == 3:
                    # Said out loud rather than swallowed. The run carries
                    # on with the other snapshots, and without this line
                    # the only evidence would be a candidate count that
                    # looks plausible and is short by a third.
                    print(f"    {snapshot} page {page}: CDX {resp.status_code} after 3 tries, "
                          f"this snapshot is truncated", file=sys.stderr)
                    resp = None
                    break
                time.sleep(2 * attempt)
            if resp is None or resp.status_code != 200 or not resp.text.strip():
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


WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"


def fetch_wayback_urls(url_pattern: str, domain_match: bool, limit: int = 200000) -> list[str]:
    """The same job fetch_cc_urls does, against the Internet Archive's
    index instead of Common Crawl's.

    Two indexes, not one, because they disagree about which hosts are
    worth keeping. Measured 2026-09-22 on pinpointhq.com: Wayback
    returned 115,531 URLs covering 1,404 tenants, and Common Crawl
    returned nothing at all for the same host across four snapshots.
    Common Crawl stays the default because it is a crawl of the open web
    and Wayback is a record of what somebody asked it to keep, which
    makes it denser on ATS hosts and thinner on the long tail.

    One request, no paging: Wayback answers the whole collapsed set in a
    single response, where CDX pages. It is also the slower of the two
    and goes down for maintenance, which is why the failure here is a
    printed line and an empty list rather than an exception.
    """
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    params = {"url": f"*.{url_pattern}" if domain_match else url_pattern,
              "output": "text", "fl": "original", "collapse": "urlkey", "limit": str(limit)}
    for attempt in (1, 2, 3):
        try:
            resp = sess.get(WAYBACK_CDX, params=params, timeout=600)
        except requests.RequestException as e:
            if attempt == 3:
                print(f"    wayback: request failed after 3 tries: {e!r}", file=sys.stderr)
                return []
            time.sleep(5 * attempt)
            continue
        if resp.status_code == 200 and resp.text.strip():
            return [line.strip() for line in resp.text.splitlines() if line.strip()]
        if resp.status_code == 200:
            # Said out loud, because an empty 200 is what this index
            # returns when it is shedding load, and it is indistinguishable
            # from "this host has no tenants" unless somebody says so.
            print("    wayback: 200 with an empty body, which means overloaded, not empty",
                  file=sys.stderr)
            if attempt == 3:
                return []
            time.sleep(10 * attempt)
            continue
        print(f"    wayback: {resp.status_code} {resp.text[:100]!r}", file=sys.stderr)
        if attempt == 3:
            return []
        time.sleep(10 * attempt)
    return []


def extract_tokens(ats: str, urls: list[str]) -> set[str]:
    if ats == "comeet":
        # uid first, so the half before the colon is the identity for
        # both these candidates and the tokens load_already_tracked reads
        # back. Case is preserved rather than folded like the tokens
        # below: a uid is "B6.00F" and the positions API is not amused by
        # "b6.00f", so only the comparison lowercases, never the value.
        out = set()
        for url in urls:
            m = COMEET_JOB_RE.search(url)
            if m:
                out.add(f"{m.group(2)}:{m.group(1)}")
        return out
    if ats == "workday":
        # One site per tenant: the one most of its crawled URLs sit
        # under. A tenant with two public sites (internal/external
        # careers) gets the busier one; the other is still one pin away.
        sites: dict[tuple[str, str], dict[str, int]] = {}
        for url in urls:
            m = WORKDAY_URL_RE.match(url)
            if not m or m.group(3).lower() in _WORKDAY_NOT_A_SITE:
                continue
            per = sites.setdefault((m.group(1).lower(), m.group(2).lower()), {})
            per[m.group(3)] = per.get(m.group(3), 0) + 1
        return {f"{tenant}:{wd}:{max(per, key=per.get)}" for (tenant, wd), per in sites.items()}
    pattern = _TOKEN_PATTERNS[ats]
    tokens = set()
    for url in urls:
        m = pattern.search(url)
        if not m:
            continue
        token = m.group(1).lower()
        # A domain match returns every URL under the host, so the tenant
        # has to be sifted out of it. Every one of these answers on www
        # and api as well, and those are not companies.
        if ats in CC_DOMAIN_MATCH and token in _NON_TENANT_SUBDOMAINS:
            continue
        tokens.add(token)
    return tokens


def load_already_tracked(ats: str) -> set[str]:
    """Every token this project already resolves for this ats, read
    from the live API (not domains.txt, which stores bare domains, not
    the tokens they resolve to) -- so discovery only surfaces genuinely
    NEW candidates, not ones already tracked under some other domain
    spelling.

    Raises rather than returning an empty set on failure -- confirmed
    live (2026-09-08): the old fail-open behavior meant a transient
    /api/companies outage (unrelated to this script, a real bug in
    api/db.py's own connection lifecycle) silently produced a whole
    batch of "new" candidates that were actually already-tracked
    companies (Wiz among them). Merging still didn't corrupt anything
    -- load_to_sqlite.py's own alias-collision check demotes an exact
    (ats,token) duplicate rather than double-tracking it -- but it
    wasted merge batch slots and inflated the queue with no-ops. main()
    now skips this ats's run entirely on failure instead, same
    fail-safe-not-fail-open shape as merge_discovered_batch.py's own
    recent_fast_poll_had_errors.
    """
    resp = requests.get(f"{SITE_ORIGIN}/api/companies", params={"resolved_only": "1", "ats": ats}, timeout=15)
    resp.raise_for_status()
    return {c["token"].split(":")[0].lower() for c in resp.json()["companies"] if c.get("token")}


_TRAILING_NUM_RE = re.compile(r"-?\d+$")
_BARE_CORP_SUFFIX_RE = re.compile(r"-?(?:inc|llc|ltd)$", re.IGNORECASE)
# TLDs common enough in company branding that a token's own trailing
# "-ai"/"-io"/etc. is plausibly standing in for "the dot", not a literal
# part of the name -- confirmed live: "reindeer-ai" (a Greenhouse token)
# is really "reindeer.ai", not "reindeer-ai.com".
_HYPHEN_TLD_RE = re.compile(r"-(ai|io|co|app|dev)$", re.IGNORECASE)

# In rough order of how often a real company actually lands on one:
# .com overwhelmingly first, then the handful of TLDs that turned up
# repeatedly researching the ones plain .com got wrong (2026-09-08).
_DOMAIN_GUESS_TLDS = ["com", "io", "ai", "co", "net", "org", "de", "fr"]


# Workable publishes a search across every board it hosts, filterable by
# location, and each result carries the employer's own website. That is
# strictly better than the Common Crawl path above for this one ATS: no
# guessed domain, and the location filter does the Israel ranking at the
# source instead of after the fact. Measured 2026-09-11: 131 jobs in
# Israel across 36 employers, 35 of which this project had never
# resolved.
#
# The account token is not in the response, but the company URL ends in
# "jobs-at-<slug>" and that slug is the token on most boards. Verified
# against humanz, autofleet, nuvei, accessfintech and cloudshare; risco
# is the counter-example, where the slug is the longer legal name and
# the token is the short one, so the domain's own first label is tried
# as a fallback.
WORKABLE_SEARCH = "https://jobs.workable.com/api/v1/jobs"
WORKABLE_PAGE_LIMIT = 20  # the endpoint refuses anything larger


def _workable_slug(company_url: str) -> str | None:
    m = re.search(r"/jobs-at-([a-z0-9-]+)", company_url or "")
    return m.group(1) if m else None


def fetch_workable_directory(sess: requests.Session, location: str,
                             max_pages: int) -> dict[str, dict]:
    """Employers hiring in `location`, keyed by candidate token."""
    out: dict[str, dict] = {}
    page_token = None
    for _ in range(max_pages):
        params = {"location": location, "limit": WORKABLE_PAGE_LIMIT}
        if page_token:
            params["pageToken"] = page_token
        try:
            r = sess.get(WORKABLE_SEARCH, params=params, timeout=30)
            r.raise_for_status()
            d = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  workable directory page failed ({e!r}), stopping here", file=sys.stderr)
            break
        for job in d.get("jobs", []):
            company = job.get("company") or {}
            slug = _workable_slug(company.get("url", ""))
            domain = _domain_of(company.get("website", ""))
            if not slug and not domain:
                continue
            token = slug or domain.split(".")[0]
            entry = out.setdefault(token, {
                "title": company.get("title") or "",
                "domain": domain,
                "jobs_seen": 0,
                # Tried in order if the first token 404s.
                "fallbacks": [t for t in (domain.split(".")[0] if domain else None,) if t and t != token],
            })
            entry["jobs_seen"] += 1
        page_token = d.get("nextPageToken")
        if not page_token:
            break
    return out


def _domain_of(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    return host.removeprefix("www.")


def _candidate_stems(token: str):
    """Ordered, most-conservative-first list of name guesses to try for
    `token`, each later verified live via _guess_domain -- generating a
    stem here is never itself proof it's right, just a candidate worth
    a real HEAD request.

    Extended 2026-09-08 for two more shapes the original trailing-digit
    strip alone missed, both found live in the same batch:
    - Bare concatenated corporate suffix, no separator ("tenableinc",
      "couchbaseinc") -- real domain is "tenable.com"/"couchbase.com".
      Limited to inc/llc/ltd specifically: rare to be part of a genuine
      brand's own stem, unlike "group"/"co"/"corp" (real counterexample
      in the same batch: "beumergroup1" strips to "beumergroup", and
      the real domain is beumergroup.com -- "group" stays, stripping it
      too would have been wrong).
    - Progressive hyphen-segment truncation ("avamere-skilled-advisors-llc"
      -> try the whole thing, then drop one trailing hyphenated segment
      at a time: "avamere-skilled-advisors", "avamere-skilled",
      "avamere") -- an ATS tenant slug is often a full legal name where
      the real domain is just the first word or two. Tried shortest-cut
      last, since a short, common word is the likeliest to coincidentally
      resolve to an unrelated site rather than the actual company.
    """
    seen = set()
    stems = []

    def add(s: str):
        if s and s not in seen:
            seen.add(s)
            stems.append(s)

    add(token)
    add(_TRAILING_NUM_RE.sub("", token))
    add(_BARE_CORP_SUFFIX_RE.sub("", token))
    parts = token.split("-")
    for cut in range(len(parts) - 1, 0, -1):
        add("-".join(parts[:cut]))
    # No-hyphen-at-all variant: "activate-interactive-pte-ltd"'s real
    # domain is "activateinteractive.com", concatenated, not hyphenated
    # -- a shape none of the hyphen-preserving stems above produce.
    if len(parts) > 1:
        add(_BARE_CORP_SUFFIX_RE.sub("", "".join(parts)))
    return stems


# A resolving domain with a 200 isn't proof anyone owns it -- confirmed
# live (2026-09-08): "wix2.com" and "redwoodmaterials.co" (both wrong
# guesses for a real company's real .com/token) resolve to the exact
# same IP and return the exact same 114-byte body: a parking-page
# redirect stub. Text markers for the handful of parking services real
# enough to matter here; the size floor alone would have caught both
# of these specific cases without needing to recognize either brand.
_PARKING_MARKERS = (
    "sedoparking", "parkingcrew", "hugedomains", "afternic", "dan.com",
    "bodis.com", "namebright", "domain is for sale", "buy this domain",
    "this domain may be for sale", "parked domain",
)


def _looks_parked(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 200:
        # A real company's homepage essentially never renders this
        # little markup -- a parking service's redirect stub does.
        return True
    lowered = stripped.lower()
    return any(marker in lowered for marker in _PARKING_MARKERS)


def _guess_domain(token: str, sess: requests.Session) -> tuple[str, bool]:
    """Best real domain for `token`, verified via a live GET request,
    not just assumed. Confirmed live (2026-09-08): plain "{token}.com"
    alone was wrong for 39 of 160 companies merged in one batch --
    mostly enterprise ATS tenant slugs (Workday/Greenhouse disambiguate
    multiple business units under one company with a trailing number or
    legal-entity suffix, e.g. "deloitte6", "tenableinc", neither ever
    meant to be read as a domain). Tries an ordered list of name guesses
    (see _candidate_stems) against a short list of common TLDs, stopping
    at the first one that actually resolves AND doesn't look like a
    parking page (see _looks_parked -- a HEAD request alone can't tell
    a parked domain from a real one, both return 200; this needs the
    body). Falls back to the plain unverified "{token}.com" guess if
    nothing else works, same as before this existed -- a genuinely
    new/small company's board should still get queued for a human
    glance rather than dropped outright just because none of these
    guesses landed.
    """
    def _try(candidate: str) -> bool:
        try:
            r = sess.get(f"https://{candidate}", timeout=5, allow_redirects=True)
            return r.status_code < 400 and not _looks_parked(r.text)
        except requests.RequestException:
            return False

    # Tried before the general stem x tld cross-product below: a strong,
    # specific signal (the token's own trailing segment looks like a TLD)
    # beats blindly trying every TLD against every stem.
    tld_match = _HYPHEN_TLD_RE.search(token)
    if tld_match:
        candidate = f"{token[:tld_match.start()]}.{tld_match.group(1).lower()}"
        if _try(candidate):
            return candidate, True

    for stem in _candidate_stems(token):
        for tld in _DOMAIN_GUESS_TLDS:
            candidate = f"{stem}.{tld}"
            if _try(candidate):
                return candidate, True
    return f"{token}.com", False


# Pinpoint's board page links to the employer's own site, so its domain
# is read rather than guessed.
#
# _guess_domain is a good guess and still wrong often enough to matter:
# measured over 160 merged companies it missed 39, because an ATS tenant
# slug is a slug, not a domain. Pinpoint does not need guessing. Every
# board page carries the company's own website in its header, its footer
# or both, the same way Workable's directory carries it. Measured over
# 80 random live tenants: 78 yielded a domain, and the two that did not
# were Pinpoint's own sandbox tenant and one board with no outbound link
# at all. Those two fall back to _guess_domain like everyone else.
#
# Hosts that belong to Pinpoint, to a CDN, or to a social network are
# not the employer. Neither is a careers subdomain, which is the same
# company one label down.
_PINPOINT_NOT_THE_EMPLOYER = re.compile(
    r"(?:^|[.])(?:pinpointhq[.]com|cloudfront[.]net|amazonaws[.]com|cloudinary[.]com|typekit[.]net"
    r"|adobe[.]com|cdnfonts[.]com|jsdelivr[.]net|unpkg[.]com|cloudflare[.]com|googleapis[.]com"
    r"|gstatic[.]com|google[.]com|doubleclick[.]net|hotjar[.]com|foresee[.]com|linkedin[.]com"
    r"|facebook[.]com|twitter[.]com|x[.]com|instagram[.]com|youtube[.]com|tiktok[.]com|threads[.]net"
    r"|bsky[.]app|pinterest[.][a-z.]+|snapchat[.]com|whatsapp[.]com|medium[.]com|github[.]com"
    r"|vimeo[.]com|spotify[.]com|apple[.]com|microsoft[.]com|glassdoor[.][a-z.]+|indeed[.][a-z.]+"
    r"|wikipedia[.]org|w3[.]org|schema[.]org|bit[.]ly)$", re.I)
_PINPOINT_HREF_RE = re.compile(r'href="https?://([a-z0-9.-]+[.][a-z]{2,})', re.I)
_CAREERS_SUBDOMAIN_RE = re.compile(
    r"^(?:jobs|careers|career|apply|talent|recruitment|recruiting|hire|join|work)[.]", re.I)


def _pinpoint_domain(sess: requests.Session, token: str) -> str | None:
    """The employer's own domain, read off their Pinpoint board page, or
    None when the page names nothing usable."""
    try:
        r = sess.get(f"https://{token}.pinpointhq.com/", timeout=15)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    hosts: dict[str, int] = {}
    for raw in _PINPOINT_HREF_RE.findall(r.text):
        host = raw.lower().rstrip(".")
        if _PINPOINT_NOT_THE_EMPLOYER.search(host):
            continue
        host = _CAREERS_SUBDOMAIN_RE.sub("", host.removeprefix("www."))
        hosts[host] = hosts.get(host, 0) + 1
    if not hosts:
        return None
    # A company that links both si.edu and trustcareers.si.edu means the
    # first one. Any host another candidate is a subdomain of wins.
    for host in list(hosts):
        for other in list(hosts):
            if other != host and other.endswith("." + host):
                hosts[host] += hosts.pop(other, 0)
    slug = re.sub(r"[^a-z0-9]", "", token.lower())

    def rank(host: str) -> tuple:
        stem = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
        return (stem == slug, slug.startswith(stem) or stem.startswith(slug), hosts[host])

    return max(hosts, key=rank)


def _comeet_where(position: dict) -> str:
    """Comeet answers with a location object on most postings and a bare
    string on some, so both shapes have to read the same way here.
    """
    loc = position.get("location")
    if isinstance(loc, dict):
        return " ".join(str(v) for v in (loc.get("name"), loc.get("city"), loc.get("country")) if v)
    return str(loc or "")


def _verify_comeet(sess: requests.Session, candidate: str) -> dict | None:
    """Turn a "uid:slug" index hit into a verified pin.

    Two fetches, and each one earns its place. The first reads the board
    page for the opaque token, which is the only thing standing between a
    Common Crawl URL and a working API call. The second is the same
    confirm-it-is-real call every other ATS here makes.

    Nothing about the result is guessed, which is unusual for this file:
    the postings carry the employer's own careers_page_url, so the domain
    is read rather than inferred from the slug. _guess_domain stays as
    the fallback for the rare board that names no page.
    """
    uid, _, slug = candidate.partition(":")
    if not uid or not slug:
        return None
    try:
        page = sess.get(f"https://www.comeet.com/jobs/{slug}/{uid}", timeout=20)
    except requests.RequestException:
        return None
    token = None
    for rx in COMEET_TOKEN_RES:
        m = rx.search(page.text)
        if m:
            token = m.group(1)
            break
    if not token:
        return None
    try:
        jobs = sess.get(COMEET_POSITIONS.format(uid=uid, token=token), timeout=25).json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(jobs, list) or not jobs:
        return None

    domain, domain_verified = "", True
    for key in ("careers_page_url", "careers_page_active_url", "careers_page_detected_url"):
        host = urlparse(str(jobs[0].get(key) or "")).netloc.lower().removeprefix("www.")
        if host and "comeet" not in host:
            domain = host
            break
    if not domain:
        domain, domain_verified = _guess_domain(slug, sess)

    return {
        "ats": "comeet",
        "token": f"{uid}:{token}",
        "job_count": len(jobs),
        "israel_job_count": sum(
            1 for p in jobs if any(kw in _comeet_where(p).lower() for kw in IL_KEYWORDS)
        ),
        "guessed_domain": domain,
        "domain_verified": domain_verified,
        "sample_titles": [str(p.get("name") or "") for p in jobs[:3]],
    }


def verify_candidate(sess: requests.Session, ats: str, token: str,
                     known: dict | None = None) -> dict | None:
    """`known` carries what a directory source already told us: the real
    domain, and any alternate tokens to try. Common Crawl gives neither,
    so it passes None and the domain is guessed as before.
    """
    if ats == "comeet":
        # Its own path entirely: comeet is deliberately absent from
        # FETCHERS (probe.py says why), so the loop below has nothing to
        # call for it.
        return _verify_comeet(sess, token)
    if ats == "workday":
        # One request, page 1: the total says the board is real, the
        # facet tree says how much of it is in Israel, and the twenty
        # newest give the sample. The full walk waits for the hourly
        # poll (scrape_workday_handler.py), which is where 1,600 tenants
        # at hundreds of pages each belongs.
        tenant, wd, site = token.split(":", 2)
        page1 = workday_page1(sess, tenant, wd, site)
        if not page1 or not page1["total"]:
            return None
        guessed_domain, domain_verified = _guess_domain(tenant, sess)
        return {
            "ats": ats,
            "token": token,
            "job_count": page1["total"],
            "israel_job_count": workday_israel_count(page1["facets"]),
            "guessed_domain": guessed_domain,
            "domain_verified": domain_verified,
            "sample_titles": [str(j.get("title") or "") for j in page1["postings"][:3]],
        }
    tokens = [token] + list((known or {}).get("fallbacks") or [])
    jobs = None
    for candidate in tokens:
        if (ats, candidate) in KNOWN_FALSE_POSITIVES:
            continue
        try:
            jobs = FETCHERS[ats](sess, candidate)
        except Exception:
            jobs = None
        if jobs:
            token = candidate
            break
    if not jobs:
        return None
    if known and known.get("domain"):
        # Straight from the employer's own listing, so there is nothing
        # to guess and nothing for a human to second-guess.
        guessed_domain, domain_verified = known["domain"], True
    elif ats == "pinpoint" and (site := _pinpoint_domain(sess, token)):
        # Same standing as the directory above: the employer put this
        # link on their own board page. See _pinpoint_domain.
        guessed_domain, domain_verified = site, True
    else:
        guessed_domain, domain_verified = _guess_domain(token, sess)
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
    ap.add_argument("--source", choices=["commoncrawl", "wayback", "directory"], default="commoncrawl",
                    help="where candidate tokens come from. 'wayback' reads the Internet "
                         "Archive's index instead of Common Crawl's, which is the only one that "
                         "covers some ATS hosts (pinpointhq.com among them). 'directory' reads "
                         "the ATS's own cross-customer job search, which carries the real domain "
                         "and filters by location; only Workable publishes one")
    ap.add_argument("--location", default="Israel", help="location filter for --source directory")
    ap.add_argument("--max-pages", type=int, default=3, help="Common Crawl CDX pages to fetch")
    ap.add_argument("--verify-limit", type=int, default=100, help="cap on how many new candidates to live-verify")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.source == "directory" and args.ats != "workable":
        print(f"--source directory is only available for workable, not {args.ats}", file=sys.stderr)
        return 2

    directory: dict[str, dict] = {}
    if args.source == "directory":
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA})
        print(f"reading Workable's own job search for {args.location} ...", file=sys.stderr)
        directory = fetch_workable_directory(sess, args.location, args.max_pages * 10)
        tokens = set(directory)
        print(f"  {len(tokens)} employers hiring there", file=sys.stderr)
    elif args.source == "wayback":
        print(f"querying the Wayback index for {CC_URL_PATTERNS[args.ats]} ...", file=sys.stderr)
        urls = fetch_wayback_urls(CC_URL_PATTERNS[args.ats], args.ats in CC_DOMAIN_MATCH)
        print(f"  {len(urls)} URLs found", file=sys.stderr)

        tokens = extract_tokens(args.ats, urls)
        print(f"  {len(tokens)} unique candidate slugs extracted", file=sys.stderr)
    else:
        print(f"querying Common Crawl for {CC_URL_PATTERNS[args.ats]} ...", file=sys.stderr)
        urls = fetch_cc_urls(CC_URL_PATTERNS[args.ats], args.max_pages,
                             indexes=CC_INDEXES_WORKDAY if args.ats == "workday" else None)
        print(f"  {len(urls)} URLs found", file=sys.stderr)

        tokens = extract_tokens(args.ats, urls)
        print(f"  {len(tokens)} unique candidate slugs extracted", file=sys.stderr)

    try:
        known = load_already_tracked(args.ats)
    except requests.RequestException as e:
        print(f"couldn't load already-tracked tokens from the live API ({e!r}) -- "
              f"stopping here rather than risk re-surfacing already-tracked companies as \"new\"",
              file=sys.stderr)
        if args.json:
            print("[]")
        return 1
    print(f"  {len(known)} already tracked for {args.ats}", file=sys.stderr)
    # Compared on the identity half rather than the whole string. A
    # Comeet candidate is "uid:slug" while load_already_tracked reads
    # back bare uids, and every other ATS's token carries no colon at
    # all, so for them this is the same subtraction it always was.
    new_tokens = sorted(t for t in tokens if t.split(":")[0].lower() not in known)
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
        return verify_candidate(sess, args.ats, token, directory.get(token))

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
