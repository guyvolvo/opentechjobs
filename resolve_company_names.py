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

Lever and Workday expose no company name on any endpoint this project
already talks to, so those stay unnamed and fall back to the domain.
Between them that is about 1% of tracked companies.

Usage:
    python resolve_company_names.py --bucket $DATA_BUCKET
    python resolve_company_names.py --known known.json --out names.json
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

TIMEOUT = 12
WORKERS = 12
UA = "Mozilla/5.0 (compatible; OpenTechJobs/1.0; +https://opentechjobs.org)"

NAMES_KEY = "company-names.json"

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


RESOLVERS = {
    "greenhouse": _greenhouse,
    "ashby": _ashby,
    "smartrecruiters": _smartrecruiters,
    "workable": _workable,
    "comeet": _comeet,
    "recruitee": _recruitee,
}


def resolve_one(entry: dict, sess: requests.Session) -> tuple[str, str | None]:
    known = REFERRAL_BOARDS.get(entry.get("domain") or "")
    if known:
        return entry["domain"], known["name"]
    fn = RESOLVERS.get(entry.get("ats") or "")
    token = entry.get("token")
    if not fn or not token:
        return entry["domain"], None
    try:
        return entry["domain"], fn(sess, token)
    except Exception:
        return entry["domain"], None


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

    todo = [e for e in known
            if e.get("ats") in RESOLVERS and (args.refresh or not names.get(e.get("domain", "")))]
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
            if name:
                names[domain] = name
                found += 1

    by_ats: dict[str, int] = {}
    for e in todo:
        if names.get(e["domain"]):
            by_ats[e["ats"]] = by_ats.get(e["ats"], 0) + 1
    print(f"resolved {found}/{len(todo)} this run: " +
          ", ".join(f"{a}={n}" for a, n in sorted(by_ats.items(), key=lambda kv: -kv[1])),
          file=sys.stderr)

    body = json.dumps(names, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if args.out:
        args.out.write_bytes(body)
        print(f"wrote {args.out} ({len(names)} names)", file=sys.stderr)
    if s3:
        s3.put_object(Bucket=args.bucket, Key=NAMES_KEY, Body=body, ContentType="application/json")
        print(f"pushed {NAMES_KEY} ({len(names)} names) to s3://{args.bucket}/{NAMES_KEY}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
