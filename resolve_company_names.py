"""Resolve each company's real name from its own ATS, once.

Why this exists. `companies.domain` is frequently not the company's real
hostname. Common Crawl discovery finds an ATS board, extracts the account
slug, and guesses {slug}.com; when that guess doesn't resolve it is kept
anyway (fixed going forward in refresh_discovery_queue.py, but ~570 rows
already carry one). So the board captions 20 real Headout listings
"headoutcareers.com", a host that has never existed. Eight of a
40-company sample were invented that way.

The honest identity is sitting in the ATS response: Greenhouse's board
endpoint says "Headout", SmartRecruiters says "Informa Group Plc.".
Reading it costs one request per company, ever, because a company's name
doesn't change. Results accumulate in company-names.json; already-named
companies are skipped on every later run, so the steady-state cost is
only whatever was discovered since.

Deliberately NOT renaming `domain` to the real host. job_id() hashes
{domain}|{ats}|{external_id} and domain is the companies primary key, so
rewriting it would change every job id for that company: its listings
would all reappear as new with first_seen reset, the old rows would
close, and the lifetime tracking PRODUCT.md calls a core differentiator
would be destroyed. Saved alerts and shared URLs filtering on the old
value would break silently too. The domain stays an opaque internal id;
the name is what gets shown.

Coverage, measured live 2026-09-09 against real boards:

    greenhouse       /boards/{token}                 .name
    ashby            jobs.ashbyhq.com/{token}        og:title, minus " Jobs"
    smartrecruiters  postings                        .company.name
    workable         widget/accounts/{token}         .name
    comeet           positions                       .company_name
    recruitee        {token}.recruitee.com/api       .company_name

    lever            job page                        <title> before " - "
    bamboohr         {token}.bamboohr.com            <title> "Login - X"
    breezy           {token}.breezy.hr               <title> "%DOC_TITLE%X"
    jazzhr           {token}.applytojob.com          <title> "X - Career Page"
    teamtailor       {token}.teamtailor.com          og:site_name
    personio         {token}.jobs.personio.de        <title> "Jobs at X" / "Jobs bei X"
    workday          first job's detail              hiringOrganization.name

The last seven were added 2026-09-25. This file used to say Lever and
Workday were "about 1% of tracked companies" and could stay unnamed;
that was true on 2026-09-09 and stopped being true when Workday
discovery grew to 3,028 tenants, which is 64% of every open listing.
Workday tenants come from workday-tenants.json rather than known.json,
which holds 33 of them, and the vendor scrapers (Amazon, Apple, Google,
Microsoft, the Israeli boards) have no endpoint to ask, so STATIC_NAMES
names them by hand.

Workday's hiringOrganization is the legal entity, which is usually the
company ("GD Information Technology, Inc.") and occasionally a payroll
shell ("WVE WVNH EMP LLC"). The legal suffix is stripped and a name that
is all capitals with a vowelless token is dropped; the domain is better
than that.

Usage:
    python resolve_company_names.py --bucket $DATA_BUCKET
    python resolve_company_names.py --known known.json --out names.json
"""

import argparse
import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

TIMEOUT = 12
WORKERS = 12
UA = "Mozilla/5.0 (compatible; OpenTechJobs/1.0; +https://opentechjobs.org)"

NAMES_KEY = "company-names.json"

# Boards with no endpoint that says who they are: the single-vendor
# scrapers and the Israeli custom boards. Applied without a request, and
# last, so a wrong resolved name can be corrected here without touching
# S3 (box/apply_company_names.py reads this too). Counts are open
# listings on 2026-09-25, which is why these few are worth a hand list.
STATIC_NAMES = {
    "amazon.com": "Amazon",                      # 14,453
    "aws.amazon.com": "Amazon Web Services",     #  8,319
    "apple.com": "Apple",                        #  4,991
    "google.com": "Google",                      #  3,243
    "microsoft.com": "Microsoft",                #  2,434
    "nvidia.com": "NVIDIA",
    "elbitsystems.com": "Elbit Systems",         #    579
    "tevapharm.com": "Teva",                     #    566
    "clalit.co.il": "Clalit",                    #    559
    "dell.com": "Dell",                          #    467
    "akamai.com": "Akamai",                      #    254
    "ness-tech.co.il": "Ness",                   #    216
    "one1.co.il": "One1",                        #    157
    "discountbank.co.il": "Discount Bank",       #     62
    "pwc.com": "PwC",                            #     51
    "iai.co.il": "IAI",
}

LEGAL_SUFFIX = re.compile(
    r"[\s,]+(inc\.?|incorporated|llc|l\.l\.c\.|ltd\.?|limited|corp\.?|corporation|co\.?|plc|"
    r"gmbh|ag|s\.?a\.?|s\.?r\.?l\.?|b\.?v\.?|pty\.?|pvt\.?|n\.?v\.?)\s*$",
    re.I,
)

# See referral_boards.py. A referral board's own name is "Referral
# Board", so the resolver below is answering honestly and still getting
# it wrong; only the override knows who the board belongs to.
try:
    from referral_boards import REFERRAL_BOARDS
except ImportError:
    REFERRAL_BOARDS = {}


def _txt(v) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v or None


def _json(sess: requests.Session, url: str):
    try:
        r = sess.get(url, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200 or "json" not in r.headers.get("Content-Type", "").lower():
        return None
    try:
        return r.json()
    except ValueError:
        return None


# Everything a title or a meta tag can tell us is in the first stretch of
# the document. The first full run read whole pages, some of them
# megabytes of SPA bundle, and sat at 98% CPU for three hours with
# nothing to show: the patterns below used to have unbounded [^>]+ runs
# that backtrack across a long attribute list. Now the read stops at
# HEAD_BYTES and every quantifier has a ceiling.
HEAD_BYTES = 256 * 1024


def _html(sess: requests.Session, url: str) -> str | None:
    try:
        with sess.get(url, timeout=TIMEOUT, headers={"Accept": "text/html"}, stream=True) as r:
            if r.status_code != 200:
                return None
            raw = b""
            for chunk in r.iter_content(16 * 1024):
                raw += chunk
                if len(raw) >= HEAD_BYTES:
                    break
            return raw.decode(r.encoding or "utf-8", errors="replace")
    except requests.RequestException:
        return None


def _title(page: str | None) -> str | None:
    m = re.search(r"<title[^>]{0,200}>([^<]{0,400})", page or "", re.I)
    return _txt(" ".join(html.unescape(m.group(1)).split())) if m else None


_META_TAG = re.compile(r"<meta\b[^>]{0,1000}>", re.I)
_META_ATTR = re.compile(r'\b(property|name|content)\s*=\s*(?:"([^"]{0,500})"|\'([^\']{0,500})\')', re.I)


def _meta(page: str | None, prop: str) -> str | None:
    """One tag at a time, attributes read once each: no pattern here can
    run back and forth across a page looking for a second quote."""
    for tag in _META_TAG.finditer(page or ""):
        attrs = {k.lower(): (a if a is not None else b) for k, a, b in _META_ATTR.findall(tag.group(0))}
        if (attrs.get("property") or attrs.get("name") or "").lower() == prop.lower() and attrs.get("content"):
            return _txt(html.unescape(attrs["content"]))
    return None


_TITLE_NOISE = {"login", "careers", "career page", "career site", "jobs", "job openings",
                "open positions", "current openings", "welcome", "home", "job board"}


def _from_title(t: str | None) -> str | None:
    """The company out of a board title like "Login - X", "X - Career
    Page", "Careers at X" or "Jobs bei X": split on the usual separators,
    drop the pieces that are only the board's own furniture, and keep
    the longest of what is left. The order and the separator vary by
    vendor and by locale; the furniture does not."""
    if not t:
        return None
    # "Jobs bei " with nothing after it is a board whose owner never set
    # a name: the prefix goes whether or not anything follows, and an
    # empty remainder is no name rather than the prefix itself.
    t = re.sub(r"^(?:jobs|careers)\s+(?:at|bei|chez|en|presso|@)\s*", "", t, flags=re.I).strip()
    pieces = [x.strip(" -–—|:·") for x in re.split(r"\s+[-–—|:·]\s+", t)]
    keep = [x for x in pieces if x and x.lower() not in _TITLE_NOISE]
    return _txt(max(keep, key=len)) if keep else None


def _strip_legal(name: str | None) -> str | None:
    """"Tenable, Inc." down to "Tenable": one trailing legal suffix off.
    Applied to every resolver's answer, since Greenhouse and the rest
    hand back the registered entity as often as Workday does."""
    if not name:
        return None
    return _txt(LEGAL_SUFFIX.sub("", name.strip()).strip(" ,"))


def _clean_org(name: str | None) -> str | None:
    """A Workday hiring organization down to a name a reader would say:
    the legal suffix off, and the acronym soup a payroll entity carries
    rejected outright, since the domain is better than that."""
    name = _strip_legal(name)
    if not name:
        return None
    tokens = name.split()
    # Two or more tokens, all capitals, none longer than four letters:
    # "WVE WVNH EMP", "OHE OHNH EMP". A brand that is an acronym is one
    # token ("IAI", "GDIT"); a run of short ones is a ledger code.
    if len(tokens) >= 2 and all(t.isupper() and len(t) <= 4 for t in tokens):
        return None
    if len(tokens) >= 2 and all(t.isupper() for t in tokens) and any(not re.search(r"[AEIOUY]", t) for t in tokens):
        return None
    return name


def _greenhouse(sess, token):
    d = _json(sess, f"https://boards-api.greenhouse.io/v1/boards/{token}")
    return _txt((d or {}).get("name")) if isinstance(d, dict) else None


def _ashby(sess, token):
    # No name anywhere in the JSON job-board API (checked: the response
    # is just {jobs, apiVersion}), but the public board page titles
    # itself "<Company> Jobs", which is the company's own chosen display
    # name rather than anything derived from the slug.
    try:
        r = sess.get(f"https://jobs.ashbyhq.com/{token}", timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    m = re.search(r'<meta property="og:title" content="([^"]+)"', r.text) or \
        re.search(r"<title>([^<]+)</title>", r.text)
    if not m:
        return None
    return _txt(re.sub(r"\s+Jobs$", "", m.group(1).strip()))


def _smartrecruiters(sess, token):
    d = _json(sess, f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1")
    posts = (d or {}).get("content") or [] if isinstance(d, dict) else []
    return _txt(((posts[0] if posts else {}).get("company") or {}).get("name"))


def _workable(sess, token):
    d = _json(sess, f"https://apply.workable.com/api/v1/widget/accounts/{token}")
    return _txt((d or {}).get("name")) if isinstance(d, dict) else None


def _comeet(sess, token):
    # token is "uid:token" for comeet, same as everywhere else.
    if ":" not in token:
        return None
    uid, ctoken = token.split(":", 1)
    d = _json(sess, f"https://www.comeet.com/careers-api/1.0/company/{uid}/positions?token={ctoken}")
    if isinstance(d, list) and d:
        return _txt(d[0].get("company_name"))
    return None


def _recruitee(sess, token):
    d = _json(sess, f"https://{token}.recruitee.com/api/offers/")
    offers = (d or {}).get("offers") or [] if isinstance(d, dict) else []
    return _txt((offers[0] if offers else {}).get("company_name"))


def _lever(sess, token):
    # The board root is an SPA titled "Lever". A posting's own page is
    # server-rendered as "<Company> - <Job title>", and the API says
    # which page to ask.
    posts = _json(sess, f"https://api.lever.co/v0/postings/{token}?mode=json&limit=1")
    url = posts[0].get("hostedUrl") if isinstance(posts, list) and posts else None
    t = _title(_html(sess, url)) if url else None
    return _txt(t.split(" - ", 1)[0]) if t and " - " in t else None


def _bamboohr(sess, token):
    # The root, not /careers: the root is titled "Login - <Company>" and
    # the careers page is titled by the company, which is sometimes
    # nothing at all.
    return _from_title(_title(_html(sess, f"https://{token}.bamboohr.com")))


def _breezy(sess, token):
    t = _title(_html(sess, f"https://{token}.breezy.hr"))
    return _txt(t.replace("%DOC_TITLE%", "")) if t else None


def _jazzhr(sess, token):
    return _from_title(_title(_html(sess, f"https://{token}.applytojob.com")))


def _teamtailor(sess, token):
    return _meta(_html(sess, f"https://{token}.teamtailor.com"), "og:site_name")


def _personio(sess, token):
    return _from_title(_title(_html(sess, f"https://{token}.jobs.personio.de")))


# Three, not five. Five was a request per job on 3,000 tenants, and the
# vote is between "the company" and "a subsidiary", which three settles
# as well as five did on the tenants checked by hand.
WORKDAY_SAMPLE = 3


def _workday(sess, token, domain=None):
    # token is "tenant:wd:site", the way workday-tenants.json spells it.
    #
    # hiringOrganization is per job and is the entity that posted it,
    # which for a group is often a subsidiary: micron.com's first listing
    # said "1580 Micron Memory Taiwan". So this reads a handful of jobs,
    # strips the numeric prefix and the legal suffix from each, and takes
    # the most common answer, preferring one that contains the domain's
    # own name when the vote is close. One job is a sample of one.
    try:
        tenant, wd, site = token.split(":", 2)
    except ValueError:
        return None
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    try:
        r = sess.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": WORKDAY_SAMPLE, "offset": 0, "searchText": ""},
                      timeout=TIMEOUT, headers={"Accept": "application/json"})
        posts = (r.json() if r.status_code == 200 else {}).get("jobPostings") or []
    except (requests.RequestException, ValueError):
        return None
    votes: dict[str, int] = {}
    for post in posts[:WORKDAY_SAMPLE]:
        path = post.get("externalPath")
        if not path:
            continue
        try:
            r = sess.get(f"{base}{path}", timeout=TIMEOUT, headers={"Accept": "application/json"})
            d = r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            continue
        raw = _txt((d.get("hiringOrganization") or {}).get("name"))
        name = _clean_org(re.sub(r"^\d+\s+", "", raw) if raw else None)
        if name:
            votes[name] = votes.get(name, 0) + 1
    if not votes:
        return None
    label = re.sub(r"[^a-z0-9]", "", (domain or "").split(".")[0].lower())
    def score(item):
        name, n = item
        own = 1 if label and label in re.sub(r"[^a-z0-9]", "", name.lower()) else 0
        return (own, n, -len(name))
    return max(votes.items(), key=score)[0]


RESOLVERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "bamboohr": _bamboohr,
    "breezy": _breezy,
    "jazzhr": _jazzhr,
    "teamtailor": _teamtailor,
    "personio": _personio,
    "workday": _workday,
    "ashby": _ashby,
    "smartrecruiters": _smartrecruiters,
    "workable": _workable,
    "comeet": _comeet,
    "recruitee": _recruitee,
}


# What a board calls itself when nobody set a company name. Lemonade's
# Ashby page is titled just "Jobs", which _ashby's " Jobs" suffix strip
# leaves alone, and Corelight's Greenhouse board is named "Job Board".
# Thirteen companies showed one of these as their name (2026-09-17). No
# name is better: the board falls back to the domain.
GENERIC_NAMES = {"jobs", "job board", "job openings", "careers", "career site",
                 "open positions", "current openings", "home"}


def resolve_one(entry: dict, sess: requests.Session) -> tuple[str, str | None]:
    known = REFERRAL_BOARDS.get(entry.get("domain") or "")
    if known:
        return entry["domain"], known["name"]
    fn = RESOLVERS.get(entry.get("ats") or "")
    token = entry.get("token")
    if not fn or not token:
        return entry["domain"], None
    try:
        name = fn(sess, token, entry.get("domain")) if fn is _workday else fn(sess, token)
    except Exception:
        return entry["domain"], None
    name = _strip_legal(name)
    if name and name.strip().lower() in GENERIC_NAMES:
        return entry["domain"], None
    return entry["domain"], name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", help="S3 bucket holding known.json and company-names.json")
    ap.add_argument("--known", type=Path, help="local known.json instead of S3")
    ap.add_argument("--out", type=Path, help="local output instead of S3")
    ap.add_argument("--limit", type=int, default=0, help="resolve at most N unnamed companies this run")
    ap.add_argument("--refresh", action="store_true",
                     help="re-resolve companies that already have a name (normally skipped)")
    args = ap.parse_args()

    s3 = None
    if args.bucket:
        import boto3
        s3 = boto3.client("s3")

    if args.known:
        known = json.loads(args.known.read_text(encoding="utf-8"))
    else:
        known = json.loads(s3.get_object(Bucket=args.bucket, Key="known.json")["Body"].read())

    names: dict[str, str] = {}
    if s3:
        try:
            names = json.loads(s3.get_object(Bucket=args.bucket, Key=NAMES_KEY)["Body"].read())
        except Exception:
            names = {}  # first run
    elif args.out and args.out.exists():
        names = json.loads(args.out.read_text(encoding="utf-8"))
    # A local copy beside the database, merged on top of S3's. A run's
    # answers are written here first, so a bucket that refuses the write
    # delays the sync rather than losing the work: the first bounded
    # batch on the box resolved 1,468 names in two minutes and lost every
    # one of them to an AccessDenied on the final put. The apply step
    # reads this file the same way.
    local = Path(os.environ.get("DATA_PATH", "/var/lib/otj/jobs.db")).with_name(NAMES_KEY)
    if s3 and local.exists():
        try:
            names.update(json.loads(local.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"ignoring unreadable {local}: {e}", file=sys.stderr)

    # Workday tenants live in their own registry, keyed the same way the
    # scraper reads them; known.json carries only a few dozen of them.
    tenants = Path(__file__).with_name("workday-tenants.json")
    if tenants.exists():
        have = {e.get("domain") for e in known if e.get("ats") == "workday"}
        for t in json.loads(tenants.read_text(encoding="utf-8")):
            if t.get("domain") and t["domain"] not in have and t.get("tenant") and t.get("wd") and t.get("site"):
                known.append({"domain": t["domain"], "ats": "workday",
                              "token": f"{t['tenant']}:{t['wd']}:{t['site']}"})
    # Skip anything already tried, named or not: a board that answered
    # with nothing is written as "" so the next batch moves on to
    # companies nobody has asked yet, rather than asking the same first
    # 1,500 every day. --refresh asks everyone again.
    todo = [e for e in known
            if e.get("ats") in RESOLVERS and (args.refresh or e.get("domain", "") not in names)]
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(known)} known companies, {len(names)} already named, {len(todo)} to resolve",
          file=sys.stderr)
    if not todo:
        return 0

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "application/json,text/html"})
    adapter = requests.adapters.HTTPAdapter(pool_connections=WORKERS * 2, pool_maxsize=WORKERS * 2)
    sess.mount("https://", adapter)

    found = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for domain, name in pool.map(lambda e: resolve_one(e, sess), todo):
            names[domain] = name or ""
            if name:
                found += 1

    by_ats: dict[str, int] = {}
    for e in todo:
        if names.get(e["domain"]):
            by_ats[e["ats"]] = by_ats.get(e["ats"], 0) + 1
    print(f"resolved {found}/{len(todo)} this run: " +
          ", ".join(f"{a}={n}" for a, n in sorted(by_ats.items(), key=lambda kv: -kv[1])),
          file=sys.stderr)

    for domain, name in STATIC_NAMES.items():
        names.setdefault(domain, name)
    body = json.dumps(names, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if args.out:
        args.out.write_bytes(body)
        print(f"wrote {args.out} ({len(names)} names)", file=sys.stderr)
    if s3:
        # Local first, bucket second. The apply step reads the local copy,
        # so the run's answers count from here even if the put fails;
        # the put failing is then a warning to fix the role, not a
        # batch to redo.
        try:
            local.write_bytes(body)
        except OSError as e:
            print(f"could not write {local}: {e}", file=sys.stderr)
        try:
            s3.put_object(Bucket=args.bucket, Key=NAMES_KEY, Body=body, ContentType="application/json")
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: {NAMES_KEY} not written to s3://{args.bucket} ({e!r}); "
                  f"the names are in {local} and will apply from there", file=sys.stderr)
        print(f"pushed {NAMES_KEY} ({len(names)} names) to s3://{args.bucket}/{NAMES_KEY}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
