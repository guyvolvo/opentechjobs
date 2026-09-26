"""Time the same API URLs against two origins, cache-busted, median of N.

    python tests/bench/api_bench.py https://oceanofjobs.com http://box:8000

Each request carries a unique `_` query parameter so no edge or browser
cache answers it: the number is the origin's own. Prints one row per
URL with p50 and max for each origin, in seconds.
"""

import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid

URLS = [
    "/api/health",
    "/api/jobs?limit=50",
    "/api/jobs?limit=50&roles=tech",
    "/api/jobs?limit=50&roles=tech&country=IL",
    "/api/jobs?limit=50&country=IL",
    "/api/jobs?limit=50&department=Security",
    "/api/jobs?limit=50&search=kubernetes",
    "/api/jobs?limit=50&search=python",
    "/api/jobs?limit=50&search=python&sort=relevance",
    "/api/jobs?limit=50&search=rust",
    "/api/facets",
    "/api/facets?country=IL",
    "/api/stats",
    "/api/companies?limit=50",
]


def timed(url: str, timeout: float = 60) -> tuple[float, int, int]:
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}_={uuid.uuid4().hex}",
                                 headers={"User-Agent": "otj-bench/1"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return time.perf_counter() - t, r.status, len(body)
    except urllib.error.HTTPError as e:
        return time.perf_counter() - t, e.code, 0
    except Exception:
        return time.perf_counter() - t, 0, 0


def main() -> int:
    origins = sys.argv[1:]
    n = 5
    print(f"{'url':52} " + " ".join(f"{o[:28]:>29}" for o in origins))
    print(f"{'':52} " + " ".join(f"{'p50':>9} {'max':>9} {'status':>9}" for _ in origins))
    rows = {}
    for path in URLS:
        cells = []
        for o in origins:
            samples = [timed(o + path) for _ in range(n)]
            secs = [s for s, _, _ in samples]
            status = samples[-1][1]
            rows.setdefault(path, {})[o] = {"p50": statistics.median(secs), "max": max(secs), "status": status}
            cells.append(f"{statistics.median(secs):9.3f} {max(secs):9.3f} {status:9d}")
        print(f"{path:52} " + " ".join(cells), flush=True)
    with open("bench-result.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
