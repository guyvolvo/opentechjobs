"""Niloosoft (Hunter) board discovery.

Every other ATS this project tracks announces itself in a URL: a
company on Greenhouse links boards.greenhouse.io/<token>, and
discover_companies.py works backwards from that. Hunter announces
nothing. Its boards sit on <tenant>.hunterhrms.com, render entirely in
the browser, and post to one shared API host that routes by a
per-tenant path. Neither half of that is guessable from the company:
Iscar's board is iscar-fr and posts to actions-iscar-hamah, Elbit's is
elbit-fr and posts to actions-elbit-friend.

Both halves are recoverable, though. Certificate transparency lists
every hunterhrms.com host anyone has ever issued a certificate for,
which is the tenant list. The action path is a literal in each board's
own JavaScript bundle. This walks the first to find the second, then
confirms the board by asking it for its jobs.

Why bother, when a first pass at Israeli ATS coverage found no Hunter
at all: the companies on it were never in the discovery queue to begin
with, so nothing was there to scan. They are large traditional
employers rather than startups, which is exactly the half of the market
a board built on startup ATSes misses. Elbit alone posts more jobs than
this project's entire Lever, Comeet and Recruitee coverage combined.

Measured 2026-09-11: 117 hosts in certificate transparency, 6 companies
with live boards, 730 open jobs. 56 hosts no longer serve at all, and
42 run an older WordPress board with no equivalent endpoint.

Usage:
    python discover_niloosoft.py --json > niloosoft-candidates.json
    python discover_niloosoft.py --host elbit-fr.hunterhrms.com
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from probe import UA, discover_niloosoft_slug, f_niloosoft, session

CT_LOG = "https://crt.sh/"
HUNTER_SUFFIX = ".hunterhrms.com"


def tenant_hosts(timeout: int = 120) -> list[str]:
    """Every hunterhrms.com host with a certificate, newest log first.

    crt.sh answers 502 under load often enough that one failure means
    nothing; it is retried rather than treated as an empty tenant list,
    which would silently look like "Hunter has no customers".
    """
    for attempt in (1, 2, 3):
        try:
            r = requests.get(CT_LOG, params={"q": "%" + HUNTER_SUFFIX, "output": "json"},
                             headers={"User-Agent": UA}, timeout=timeout)
            rows = r.json()
            break
        except (requests.RequestException, ValueError) as e:
            if attempt == 3:
                print(f"certificate transparency unavailable after 3 tries ({e!r})", file=sys.stderr)
                return []
    hosts = set()
    for row in rows:
        for name in (row.get("name_value") or "").split("\n"):
            name = name.strip().lower()
            if name.endswith(HUNTER_SUFFIX) and "*" not in name:
                hosts.add(name)
    return sorted(hosts)


def check_host(host: str) -> dict | None:
    sess = session()
    sess.headers["User-Agent"] = UA
    slug = discover_niloosoft_slug(sess, host)
    if not slug:
        return None
    token = f"{host}:{slug}"
    try:
        jobs = f_niloosoft(sess, token)
    except Exception:
        return None
    if not jobs:
        return None
    return {
        "ats": "niloosoft",
        "token": token,
        "host": host,
        "slug": slug,
        "job_count": len(jobs),
        "sample_titles": [j.title for j in jobs[:3]],
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", action="append",
                    help="check only these hosts instead of the whole certificate log")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    hosts = args.host or tenant_hosts()
    if not hosts:
        if args.json:
            print("[]")
        return 1
    print(f"{len(hosts)} hunterhrms.com hosts to check ...", file=sys.stderr)

    # Deliberately narrow. Most of these hosts are dead certificates that
    # hang rather than refuse, and the live ones all answer from one
    # shared API that nobody else is paying to run.
    found = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for r in pool.map(check_host, hosts):
            if r:
                found.append(r)
                print(f"  {r['host']:34} {r['slug']:24} {r['job_count']:>4} jobs", file=sys.stderr)

    found.sort(key=lambda r: -r["job_count"])
    print(f"{len(found)} live boards, {sum(r['job_count'] for r in found)} jobs", file=sys.stderr)
    if args.json:
        print(json.dumps(found, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
