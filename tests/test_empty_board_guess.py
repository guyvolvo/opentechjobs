"""An empty board found by guessing is not a company's board.

Measured 2026-09-18 over 60 Israeli hosts sampled from Common Crawl:
seven "matches", five of them empty boards on a token taken off a
subdomain (workable:school for school.walla.co.il, workable:online for
online.study.co.il, lever:career for career.bbalev.co.il). Recording
those is how 331 companies, Dell and IBM among them, ended up on empty
Workable slugs that then masked their real boards.

Run directly, no framework:  python tests/test_empty_board_guess.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import probe  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def job(token, title="Backend Engineer", posted="2026-09-10T00:00:00+00:00"):
    return probe.Job("workable", token, f"{token}-1", title, "Tel Aviv, Israel",
                     "https://example.test/job", posted)


def resolve_with(boards, domain="example.co.il", tokens=("example",)):
    """resolve() with the guess loop answering from `boards`, and every
    other tier switched off."""
    saved = (probe.FETCHERS, probe.token_candidates, probe.PINS, dict(probe.HINTS),
             probe.SCRAPE_COMEET, probe.SCRAPE_EMBED)
    probe.FETCHERS = {"workable": lambda sess, token: boards.get(token)}
    probe.token_candidates = lambda d: list(tokens)
    probe.PINS = {}
    probe.HINTS.clear()
    probe.SCRAPE_COMEET = False
    probe.SCRAPE_EMBED = False
    try:
        return probe.resolve(domain, None)
    finally:
        (probe.FETCHERS, probe.token_candidates, probe.PINS, _hints,
         probe.SCRAPE_COMEET, probe.SCRAPE_EMBED) = saved
        probe.HINTS.clear()
        probe.HINTS.update(_hints)


# The case that cost 331 companies their real boards.
res = resolve_with({"example": []})
check("an empty guessed board is not recorded as the company's ats", res.ats is None, repr(res.ats))
check("and it is inconclusive, so the loader keeps whatever it already had",
      res.retryable is True, repr(res.retryable))
check("the error says which board it was", "workable:example" in (res.error or ""), repr(res.error))

# A board with jobs still resolves, exactly as before.
res = resolve_with({"example": [job("example")]})
check("a board with jobs still resolves", res.ats == "workable" and res.job_count == 1,
      repr((res.ats, res.job_count)))
check("and is a confident answer", not res.retryable)

# A real board beats an empty one whatever order they are tried in.
# Not the token "real": that one is in KNOWN_FALSE_POSITIVES for real.dev
# and is skipped before any fetch, which is the correct behaviour and
# made this test look like a bug the first time it ran.
res = resolve_with({"hollow": [], "livedboard": [job("livedboard")]}, tokens=("hollow", "livedboard"))
check("a real board wins over an empty one tried first", res.token == "livedboard", repr(res.token))
res = resolve_with({"hollow": [], "livedboard": [job("livedboard")]}, tokens=("livedboard", "hollow"))
check("and over one tried after it", res.token == "livedboard", repr(res.token))

# Nothing at all is still a confident miss, which is what --prune-stale reads.
res = resolve_with({})
check("no board at all stays a confident miss",
      res.ats is None and not res.retryable and "no ATS matched" in (res.error or ""),
      repr((res.retryable, res.error)))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
