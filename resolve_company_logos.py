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
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from company_logo import UA, resolve_logo  # noqa: E402

WORKERS = 10
LOGOS_KEY = "company-logos.json"

# See referral_boards.py. referralsuseonly.com is not a website, so every
# logo path that starts from the domain is looking somewhere that does
# not exist. Ask the real company's domain instead.
try:
    from referral_boards import REFERRAL_BOARDS
except ImportError:
    REFERRAL_BOARDS = {}


def resolve_one(entry: dict, sess: requests.Session) -> tuple[str, dict]:
    domain = entry.get("domain", "")
    look_at = REFERRAL_BOARDS.get(domain, {}).get("logo_domain", domain)
    try:
        # The ATS is still passed: a referral board carries the company's
        # own logo on Greenhouse, which is a better source than the
        # website favicon either way.
        url, source = resolve_logo(sess, look_at, entry.get("ats"), entry.get("token"))
    except Exception:
        # An unreachable host is not an answer, so leave it out of the
        # file entirely and let the next run try again.
        return domain, {}
    return domain, {"url": url, "source": source}


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

    todo = [e for e in known
            if e.get("domain") and (args.refresh or e["domain"] not in logos)]
    if args.limit:
        todo = todo[:args.limit]

    have = sum(1 for v in logos.values() if v.get("url"))
    print(f"{len(known)} known companies, {have} already have a logo, {len(todo)} to resolve",
          file=sys.stderr)
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
            logos[domain] = entry
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
