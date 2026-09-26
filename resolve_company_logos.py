"""Resolve each company's logo once, and keep the answers in one file.

Same shape and the same reasoning as resolve_company_names.py, for the
same reason: a logo is resolved once and then essentially never changes,
while the partitions the board is built from are rewritten every few
minutes by a fast poll that has no business spending five extra requests
per company on a picture. Storing the answer in the partitions would
mean either paying that cost forever or losing the answer on the next
rotation.

So logos live in company-logos.json, keyed by domain, and
merge_partitions stamps them onto the merged snapshot the same way it
stamps names. A company already resolved is skipped on later runs, so
the steady-state cost is only whatever discovery has added since.

company_logo.py does the actual looking, ATS first. See its docstring
for why guessing the company's own domain is the fallback and not the
plan.

Entries record the source tier as well as the URL, because "this company
has no logo anywhere" and "we have not looked yet" are different facts
and only one of them is worth retrying:

    {"acme.com": {"url": "https://...", "source": "ats"}}

A company whose every tier came up empty is stored with a null url, so
it is not retried on every run forever. --refresh re-checks everything.

Usage:
    python resolve_company_logos.py --bucket $DATA_BUCKET
    python resolve_company_logos.py --known known.json --out logos.json --limit 50
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from company_logo import UA, resolve_logo  # noqa: E402

WORKERS = 10

# How long a company with no logo is left alone before being asked
# again, and the most such retries any one run will do.
RETRY_MISSES_AFTER_DAYS = 7
MAX_RETRIES_PER_RUN = 400
LOGOS_KEY = "company-logos.json"

# Bumped when a change to company_logo.py can reject a logo it used to
# accept. Every logo stamped with an older version is checked again,
# once. Version 2 added PLACEHOLDER_ICONS: Google had been handing back
# GoDaddy's logo, and other parking and hosting icons, for parked
# domains, and those were stored as found and never looked at again,
# because a company that has a logo is otherwise settled.
#
# Version 3, 2026-09-21, is the same lesson with the scope corrected.
# Version 2 only rechecked Google-tier logos, on the reasoning that
# nothing about the new checks changed what the site tier returns. That
# was wrong: a site favicon goes through exactly the same
# PLACEHOLDER_ICONS gate, so every site logo stored before a fingerprint
# was added kept it forever. Hashing all 3,843 site logos at once found
# 264 companies wearing an image that belonged to somebody else, two
# clusters of them the WordPress default, which version 2 already
# thought it had dealt with.
#
# Version 4, the same day, because version 3 was not the end of it.
# Rejecting the WordPress default from a company's own site moved all
# of those companies onto Google's favicon service, which served the
# identical mark at a size no fingerprint had seen. company_logo.py now
# compares what the picture looks like rather than only its bytes, and
# everything accepted under 3 has to be looked at through that.
LOGO_CHECK_VERSION = 5  # 5: loader/placeholder_logos.py and the parked-domain Cloudflare icon (2026-09-26)

# See referral_boards.py. referralsuseonly.com is not a website, so every
# logo path that starts from the domain is looking somewhere that does
# not exist. Ask the real company's domain instead.
from company_aliases import logo_domain


def resolve_one(entry: dict, sess: requests.Session) -> tuple[str, dict]:
    domain = entry.get("domain", "")
    look_at = logo_domain(domain)
    try:
        # The ATS is still passed: a referral board carries the company's
        # own logo on Greenhouse, which is a better source than the
        # website favicon either way.
        url, source = resolve_logo(sess, look_at, entry.get("ats"), entry.get("token"))
    except Exception:
        # An unreachable host is not an answer, so leave it out of the
        # file entirely and let the next run try again.
        return domain, {}
    # Stamped even on a miss, so the retry pacing above can tell a
    # company nobody has looked at from one that was looked at and had
    # nothing to give.
    return domain, {"url": url, "source": source,
                    "tried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "looked_at": look_at, "check": LOGO_CHECK_VERSION}


def needs_recheck(domain: str, entry: dict) -> bool:
    """Is this found logo one a later fix may have made wrong?

    Two ways. An alias was added after the logo was stored, so it was
    looked up at the wrong domain (entries from before looked_at existed
    were looked up at the domain itself). Or it was accepted before the
    current checks existed, which is where the parked-domain and site
    builder icons were hiding.

    Site logos are rechecked as well as Google ones, which is the fix in
    version 3. They run through the same placeholder gate, so leaving
    them out meant a fingerprint added today never reached the thousands
    of favicons stored yesterday.

    ATS logos are still left alone. Those come from the company's own
    account on its own hiring system, which is the one tier that cannot
    hand back somebody else's picture.
    """
    if not entry.get("url"):
        return False
    if entry.get("looked_at", domain) != logo_domain(domain):
        return True
    return (entry.get("source") in ("google", "site")
            and entry.get("check", 1) < LOGO_CHECK_VERSION)


def merge_entry(logos: dict, domain: str, entry: dict) -> None:
    """Store a fresh answer, and say so when it takes a logo away.

    The loaders never write a null over a stored logo, deliberately, so a
    rejected logo would stay on the board forever. "cleared" names the URL
    to take down. Only for a Google-tier logo or one looked up at the wrong
    domain, the two kinds needs_recheck exists for: a good ATS or site logo
    that fails a later lookup on a bad network day is kept, not cleared.
    """
    previous = logos.get(domain) or {}
    if previous.get("url") and not entry.get("url") and (
        previous.get("source") == "google"
        or previous.get("looked_at", domain) != entry.get("looked_at", domain)
    ):
        entry["cleared"] = previous["url"]
    logos[domain] = entry


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", help="S3 bucket holding known.json and company-logos.json")
    ap.add_argument("--known", type=Path, help="local known.json instead of S3")
    ap.add_argument("--out", type=Path, help="local output instead of S3")
    ap.add_argument("--limit", type=int, default=0, help="resolve at most N companies this run")
    ap.add_argument("--refresh", action="store_true",
                     help="re-resolve companies already in the file (normally skipped)")
    args = ap.parse_args()

    s3 = None
    if args.bucket:
        import boto3
        s3 = boto3.client("s3")

    if args.known:
        known = json.loads(args.known.read_text(encoding="utf-8"))
    else:
        known = json.loads(s3.get_object(Bucket=args.bucket, Key="known.json")["Body"].read())

    logos: dict[str, dict] = {}
    if s3:
        try:
            logos = json.loads(s3.get_object(Bucket=args.bucket, Key=LOGOS_KEY)["Body"].read())
        except Exception:
            logos = {}  # first run
    elif args.out and args.out.exists():
        logos = json.loads(args.out.read_text(encoding="utf-8"))

    # A company that HAS a logo is settled. A company that does not is
    # not: it may have had no site the day we asked, or its entry may
    # predate a fix to the resolver. Those get looked at again.
    #
    # This was the bug behind two separate ones. A miss used to be stored
    # as {"url": None, "source": "none"}, which is a truthy dict, so the
    # domain counted as done and was skipped on every later run. Six
    # companies kept their monogram forever, and a fix shipped earlier
    # today for referral-board logos could never take effect, because the
    # only domains it applied to had already been written off.
    #
    # Retries are paced rather than run every time: a site that had no
    # icon yesterday probably has none today, and there are enough of
    # these to matter. RETRY_MISSES_AFTER_DAYS keeps the daily run's
    # extra work bounded, and MAX_RETRIES_PER_RUN bounds it absolutely.
    now = datetime.now(timezone.utc)

    def is_stale_miss(entry: dict) -> bool:
        if entry.get("url"):
            return False
        tried = entry.get("tried_at")
        if not tried:
            return True  # written before this field existed
        try:
            age = now - datetime.fromisoformat(tried)
        except ValueError:
            return True
        return age > timedelta(days=RETRY_MISSES_AFTER_DAYS)

    fresh = [e for e in known if e.get("domain") and e["domain"] not in logos]
    retries = [] if args.refresh else [
        e for e in known
        if e.get("domain") and e["domain"] in logos and is_stale_miss(logos[e["domain"]])
    ]
    rechecks = [] if args.refresh else [
        e for e in known
        if e.get("domain") and e["domain"] in logos and needs_recheck(e["domain"], logos[e["domain"]])
    ]
    todo = known if args.refresh else fresh + retries[:MAX_RETRIES_PER_RUN] + rechecks
    todo = [e for e in todo if e.get("domain")]
    if args.limit:
        todo = todo[:args.limit]

    have = sum(1 for v in logos.values() if v.get("url"))
    print(f"{len(known)} known companies, {have} already have a logo, {len(todo)} to resolve "
          f"({len(rechecks)} rechecks)", file=sys.stderr)
    if not todo:
        return 0

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    adapter = requests.adapters.HTTPAdapter(pool_connections=WORKERS * 2, pool_maxsize=WORKERS * 2)
    sess.mount("https://", adapter)

    by_source: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for domain, entry in pool.map(lambda e: resolve_one(e, sess), todo):
            if not entry:
                continue
            merge_entry(logos, domain, entry)
            by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1

    print("this run: " + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items(),
                                                                 key=lambda kv: -kv[1])),
          file=sys.stderr)
    total = sum(1 for v in logos.values() if v.get("url"))
    print(f"{total}/{len(logos)} companies in the file now have a logo", file=sys.stderr)

    body = json.dumps(logos, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if args.out:
        args.out.write_bytes(body)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        s3.put_object(Bucket=args.bucket, Key=LOGOS_KEY, Body=body,
                      ContentType="application/json")
        print(f"wrote s3://{args.bucket}/{LOGOS_KEY}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
