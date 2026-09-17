"""keep_companies_added_meanwhile: the daily sweep's known.json export must
not wipe companies added while it was running.

2026-09-17: the sweep ran from 08:38 to 10:53 and replaced known.json
with its own export, taking it from 6,900 companies to 6,017. Atera and
Paragon, pinned during the run, went with the rest.

Run directly, no framework:  python tests/test_known_merge.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

import load_to_sqlite as lts  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def row(domain, ats="greenhouse"):
    return {"domain": domain, "ats": ats, "token": domain.split(".")[0]}


tmp = Path(tempfile.mkdtemp())
try:
    # When the sweep started.
    baseline = [row("old.com"), row("dropped.com"), row("swept-away.com"),
                row("eramtalent.com", "workable")]
    # What the sweep probed (domains.txt at checkout) and resolved.
    resolved = [{"domain": "old.com", "ats": "greenhouse"},
                {"domain": "swept-away.com", "ats": None, "retryable": False},
                # Its Workable board did not answer the sweep.
                {"domain": "eramtalent.com", "ats": None, "retryable": True}]
    export = [row("old.com")]
    # known.json in S3 by the time the sweep finished: the batch merge and a
    # targeted run added two companies, and one of them the sweep also
    # probed and found nothing for.
    live = baseline + [row("atera.com", "comeet"), row("paragon-solutions.invalid", "comeet")]

    (tmp / "known-at-start.json").write_text(json.dumps(baseline))
    (tmp / "resolved.json").write_text(json.dumps(resolved))
    (tmp / "known.json").write_text(json.dumps(export))

    def fake_pull(bucket, key, dest, body=live):
        Path(dest).write_text(json.dumps(body))
        return True, "etag"

    real_pull = lts.s3_pull
    lts.s3_pull = fake_pull
    try:
        n = lts.keep_companies_added_meanwhile("b", "known.json", tmp / "known.json",
                                               tmp / "known-at-start.json", tmp / "resolved.json")
    finally:
        lts.s3_pull = real_pull
    out = {e["domain"] for e in json.loads((tmp / "known.json").read_text())}

    check("companies added during the run are kept", {"atera.com", "paragon-solutions.invalid"} <= out, repr(out))
    check("a board that did not answer the run is kept", "eramtalent.com" in out, repr(out))
    check("the count says how many", n == 3, repr(n))
    check("the run's own answer stays", "old.com" in out)
    check("a company pruned from domains.txt stays pruned", "dropped.com" not in out, repr(out))
    check("a company the run probed and lost stays lost", "swept-away.com" not in out, repr(out))

    # Nothing in S3: nothing to keep, and the export is untouched.
    (tmp / "known.json").write_text(json.dumps(export))
    lts.s3_pull = lambda bucket, key, dest: (False, None)
    try:
        n = lts.keep_companies_added_meanwhile("b", "known.json", tmp / "known.json",
                                               tmp / "known-at-start.json", tmp / "resolved.json")
    finally:
        lts.s3_pull = real_pull
    check("no live file keeps nothing", n == 0 and json.loads((tmp / "known.json").read_text()) == export)

    # No baseline: keep nothing rather than guess which companies are new.
    lts.s3_pull = fake_pull
    try:
        n = lts.keep_companies_added_meanwhile("b", "known.json", tmp / "known.json",
                                               tmp / "missing.json", tmp / "resolved.json")
    finally:
        lts.s3_pull = real_pull
    check("no baseline keeps nothing", n == 0)

    wf = (ROOT / ".github/workflows/scrape-discover.yml").read_text(encoding="utf-8")
    check("the full sweep passes a baseline", "--prune-stale --known-baseline known-at-start.json" in wf)
    check("the baseline is copied before the load overwrites known.json", "cp known.json known-at-start.json" in wf)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# probe.resolve: a hinted board that does not answer is inconclusive.
sys.path.insert(0, str(ROOT))
import probe  # noqa: E402

saved = (dict(probe.HINTS), probe.FETCHERS, probe.token_candidates, probe.PINS)
try:
    answers = {}
    probe.FETCHERS = {"workable": lambda sess, token: answers.get(token)}
    probe.token_candidates = lambda domain: []   # nothing to guess
    probe.PINS = {}
    probe.HINTS.clear()
    probe.HINTS["eramtalent.com"] = {"ats": "workable", "token": "eramtalent-1"}

    res = probe.resolve("eramtalent.com", None)
    check("an unanswered hint is retryable", res.ats is None and res.retryable, repr((res.ats, res.retryable, res.error)))
    check("and says which board", "eramtalent-1" in (res.error or ""), repr(res.error))

    probe.HINTS.clear()
    res = probe.resolve("eramtalent.com", None)
    check("no hint and no match is a confident miss", res.ats is None and not res.retryable, repr(res.retryable))
finally:
    probe.HINTS.clear()
    probe.HINTS.update(saved[0])
    probe.FETCHERS, probe.token_candidates, probe.PINS = saved[1], saved[2], saved[3]

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
