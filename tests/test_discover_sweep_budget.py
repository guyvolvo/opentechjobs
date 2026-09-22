"""The nightly discover sweep stopped finishing, and then we made it bigger.

Two incidents, one file, because the fix for the second is what makes the
first survivable.

Part A. Pvalyou's Israeli Startup Ecosystem snapshot
(github.com/pvalyou/ise-data) lists 4,009 Israeli companies. 3,987 of
them carry a parseable domain, and 3,514 of those were not yet in
domains.txt or the companies table. They are in domains.txt now as a
seed list of domains and nothing else: none of that file's 21,005 job
rows is stored or republished, because PRODUCT.md's whole claim is that
a listing is ground-truthed against the employer's own ATS. The snapshot
is CC BY-NC 4.0, so the credit and the non-commercial sentence live in
the file next to the data. This pins all three: no domain added twice,
no job row smuggled in as a URL, and the licence note still there for
whoever reads the file next.

Part B. domains.txt went from 4,271 domains to 15,602. The run that
covered the 4,271 took 3h40m, and .github/workflows/scrape-discover.yml
now sets its own 330-minute limit under GitHub's 6-hour cap. Measured
2026-09-22: 6,582 of 11,996 companies had not been re-checked in two
days, and September 9th through 11th produced nothing at all, because
the job died after probing and before its first S3 write.

The cost is all in guessing. A domain with a hint is one request; a
domain without one is probe.py's 45-second search and then two
careers-page scrapes, and it pays that again every night, forever. So
the guessing is sharded over four days and capped by a wall-clock
deadline, and every domain a run does not reach comes back retryable.
That word carries the whole safety argument. load_to_sqlite reads a
retryable non-answer as no evidence, leaves the company's ats, token and
open jobs alone, and still counts the domain as covered so --prune-stale
does not demote it for being missing. These tests pin that contract. A
deferral the loader read as a MISS would close three quarters of the
board every night.

Run directly, no framework:  python tests/test_discover_sweep_budget.py
"""

import json
import re
import sqlite3
import sys
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

import probe  # noqa: E402
from load_to_sqlite import load_resolved, open_db, prune_stale_companies  # noqa: E402

TS_OLD = "2026-09-01T00:00:00+00:00"
# Fresh on purpose. _match_is_fresh treats a board whose newest posting
# is over STALE_MATCH_DAYS old as not worth stopping the search for, and
# that rule is deliberately left alone for hints: an abandoned board
# should not block the hunt for the company's current one.
NOW = datetime.now(timezone.utc).isoformat()

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{'' if ok else '  -- ' + detail}")
    if not ok:
        failures.append(name)


DOMAINS_TXT = (ROOT / "domains.txt").read_text(encoding="utf-8-sig").splitlines()
LISTED = [line.strip() for line in DOMAINS_TXT
          if line.strip() and not line.lstrip().startswith("#")]

print("\n-- the seed list --")

seen: dict[str, int] = {}
dupes = []
for i, d in enumerate(LISTED, 1):
    key = d.lower()
    if key in seen:
        dupes.append(f"{d} (again at line {i}, first at {seen[key]})")
    seen[key] = i
check("domains.txt lists no domain twice", not dupes, "; ".join(dupes[:5]))


def new_against(candidates, listed):
    """The rule the seeding ran under: a candidate already in
    domains.txt, under any casing, is not a new domain.
    """
    have = {d.lower() for d in listed}
    return [c for c in candidates if c.lower() not in have]


# A domain already in the file, the same one shouting, and one that is
# genuinely not there. Only the last survives.
check("the dedupe drops a domain already in domains.txt",
      new_against(["monday.com", "MONDAY.COM", "not-a-real-company-xyzzy.example"], LISTED)
      == ["not-a-real-company-xyzzy.example"])
check("feeding domains.txt back through the dedupe adds nothing",
      new_against(LISTED, LISTED) == [])

# Every line is a bare host. A URL, a job id or a title here would mean
# a row from the snapshot leaked in where only a domain was meant to.
# Underscores are allowed although DNS does not allow them, because six
# lines predating this file carry them (clover_security.com and friends,
# from an older Common Crawl batch). They are almost certainly junk, but
# they are not what this test is about and failing on them would hide
# what is.
BARE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z]{2,}$")
not_hosts = [d for d in LISTED if not BARE_HOST.match(d)]
check("every listed line is a bare host, no job rows", not not_hosts, ", ".join(not_hosts[:5]))

COMMENTS = "\n".join(line for line in DOMAINS_TXT if line.lstrip().startswith("#"))
check("the seed block credits Pvalyou",
      "github.com/pvalyou/ise-data" in COMMENTS and "Pvalyou" in COMMENTS)
check("the seed block quotes the non-commercial clause",
      "You may not use it commercially" in COMMENTS and "CC BY-NC 4.0" in COMMENTS)

print("\n-- the guess shard --")

sample = LISTED[:2000]


def shard_of(domain: str, total: int) -> int:
    return zlib.crc32(domain.encode("utf-8")) % total


probe.GUESS_SHARD = None
check("no shard set means every domain is guessable",
      all(probe._in_guess_shard(d) for d in sample))

covered: dict[str, int] = {}
for index in range(4):
    probe.GUESS_SHARD = (index, 4)
    for d in sample:
        if probe._in_guess_shard(d):
            covered[d] = covered.get(d, 0) + 1
check("four runs cover every domain exactly once",
      len(covered) == len(set(sample)) and set(covered.values()) == {1},
      f"{len(covered)} of {len(set(sample))}, counts {sorted(set(covered.values()))}")

# crc32 and not hash(): PYTHONHASHSEED randomises str hashing per
# process, so a hash-based shard would reshuffle every run and let a
# domain go weeks without being picked. crc32 is a fixed property of the
# string, so a refactor that swaps it fails here instead of quietly
# starving domains.
probe.GUESS_SHARD = (zlib.crc32(b"monday.com") % 4, 4)
check("shard membership is a fixed property of the domain",
      probe._in_guess_shard("monday.com") and shard_of("monday.com", 4) == zlib.crc32(b"monday.com") % 4)

accepted = []
for text in ("4/4", "-1/4", "2", "two/four", "1/0", ""):
    try:
        probe.parse_guess_shard(text)
        accepted.append(text)
    except SystemExit:
        pass
check("parse_guess_shard rejects a typo instead of silently skipping work",
      not accepted, f"accepted {accepted}")
check("parse_guess_shard reads a good one", probe.parse_guess_shard("3/4") == (3, 4))

probe.GUESS_SHARD = None

print("\n-- what a deferred domain hands back --")

res = probe._deferred(probe.Resolution(domain="acme.com"), "not in this run's guess shard (1/4)")
check("a deferral is a retryable non-answer",
      res.ats is None and res.retryable is True and bool(res.error))

print("\n-- what the loader does with it --")


def seed_db(db_path: Path) -> sqlite3.Connection:
    """A company resolved on an earlier run, with three open jobs."""
    conn = open_db(db_path)
    with conn:
        conn.execute(
            "INSERT INTO companies (domain, ats, token, confidence, job_count, tried, first_seen, last_checked) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("acme.com", "greenhouse", "acme", "verified", 3, 1, TS_OLD, TS_OLD),
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO jobs (id, company_domain, ats, external_id, title, confidence, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"acme-{i}", "acme.com", "greenhouse", str(i), f"Engineer {i}", "verified", TS_OLD, TS_OLD),
            )
    return conn


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    conn = seed_db(tmp / "jobs.db")
    payload = [{"domain": "acme.com", "ats": None, "token": None, "jobs": [],
                "job_count": 0, "tried": 0, "retryable": True,
                "error": "not in this run's guess shard (1/4)"}]
    path = tmp / "resolved.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with conn:
        current = load_resolved(conn, path)

    row = conn.execute("SELECT ats, token, job_count FROM companies WHERE domain = 'acme.com'").fetchone()
    open_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE company_domain = 'acme.com' AND closed_at IS NULL").fetchone()[0]
    check("a deferred company keeps its ats and token",
          (row["ats"], row["token"]) == ("greenhouse", "acme"), str(tuple(row)))
    check("a deferred company keeps its job_count", row["job_count"] == 3, str(row["job_count"]))
    check("a deferred company keeps its open jobs", open_jobs == 3, str(open_jobs))

    # The other half, and the one that would hurt most quietly. A domain
    # this run chose not to guess at is still a domain the project
    # tracks. If it fell out of the set --prune-stale compares against,
    # three quarters of every resolved company would be demoted and
    # closed out every night.
    check("a deferred domain still counts as covered by the run", "acme.com" in current, str(current))
    with conn:
        pruned = prune_stale_companies(conn, current, TS_OLD)
    still = conn.execute("SELECT ats FROM companies WHERE domain = 'acme.com'").fetchone()["ats"]
    check("--prune-stale does not demote a deferred company",
          pruned == 0 and still == "greenhouse", f"pruned={pruned} ats={still}")
    # Windows will not delete an open file, and TemporaryDirectory
    # cleanup runs before the connection would otherwise be collected.
    conn.close()

print("\n-- the hint path --")


class Poisoned(Exception):
    pass


def poisoned(*a, **kw):
    raise Poisoned("guessing started even though a hint had answered")


real_fetch, real_candidates = probe._fetch_by_ats, probe.token_candidates
real_hints, real_comeet, real_embed = probe.HINTS, probe.SCRAPE_COMEET, probe.SCRAPE_EMBED
try:
    probe.HINTS = {"acme.com": {"ats": "greenhouse", "token": "acme"}}
    probe.token_candidates = poisoned
    probe.SCRAPE_COMEET = probe.SCRAPE_EMBED = False

    # An empty answer from a hint is this company's board saying zero,
    # not a dead end. It used to fall through to the full search. 444
    # known companies paid for that nightly, and where the token was one
    # guessing cannot reach (every Comeet pin, workable:eramtalent-1) the
    # search ended at the non-retryable "no ATS matched any token
    # candidate", which cleared a working company's ats.
    probe._fetch_by_ats = lambda sess, ats, token: []
    r = probe._resolve_board("acme.com", None)
    check("an empty board from a hint ends the search",
          r.ats == "greenhouse" and r.token == "acme" and r.job_count == 0,
          f"ats={r.ats} error={r.error}")

    probe._fetch_by_ats = lambda sess, ats, token: [probe.Job(
        ats="greenhouse", token="acme", external_id="1",
        title="Engineer", location="Tel Aviv",
        url="https://example.invalid/1", posted_at=NOW)]
    r = probe._resolve_board("acme.com", None)
    check("a populated board from a hint still ends the search",
          r.ats == "greenhouse" and r.job_count == 1, f"ats={r.ats} error={r.error}")

    # No answer at all is a different thing and must still fall through.
    # A rate limit looks exactly like this, and a company that genuinely
    # moved ATS is only found by searching.
    probe._fetch_by_ats = lambda sess, ats, token: None
    try:
        probe._resolve_board("acme.com", None)
        check("an unanswered hint still falls through to guessing", False, "it never guessed")
    except Poisoned:
        check("an unanswered hint still falls through to guessing", True)

    # And a domain outside the shard never reaches the guess loop at all,
    # hint or no hint.
    probe.GUESS_SHARD = ((zlib.crc32(b"acme.com") % 4 + 1) % 4, 4)
    probe._fetch_by_ats = lambda sess, ats, token: None
    r = probe._resolve_board("acme.com", None)
    check("a domain outside the shard is deferred, not guessed at",
          r.ats is None and r.retryable is True and "shard" in (r.error or ""), str(r.error))
finally:
    probe._fetch_by_ats, probe.token_candidates = real_fetch, real_candidates
    probe.HINTS, probe.SCRAPE_COMEET, probe.SCRAPE_EMBED = real_hints, real_comeet, real_embed
    probe.GUESS_SHARD = None

print("\n-- compound tokens the hint path could not reach --")

seen_args: dict[str, tuple] = {}


def fake_comeet(sess, uid, token):
    seen_args["comeet"] = (uid, token)
    return []


real_comeet_pin = probe._fetch_comeet_pin
try:
    probe._fetch_comeet_pin = fake_comeet
    out = probe._fetch_by_ats(None, "comeet", "27.006:7262AE41")
    check("_fetch_by_ats splits a Comeet uid:token",
          out == [] and seen_args.get("comeet") == ("27.006", "7262AE41"), str(seen_args))
finally:
    probe._fetch_comeet_pin = real_comeet_pin

check("comeet, workday and jsonld are absent from FETCHERS, which is why the helper exists",
      not {"comeet", "workday", "jsonld"} & set(probe.FETCHERS))
check("an ats nobody knows is None, not a crash",
      probe._fetch_by_ats(None, "nosuchats", "whatever") is None)

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all passed")
