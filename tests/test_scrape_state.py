"""Per-board poll scheduling: what backs off, what resets, what must not.

The saving here comes from polling quiet boards less often, so the risk
is a board that goes quiet, drifts to the twenty-minute ceiling, starts
hiring again, and nobody notices for twenty minutes on every subsequent
poll. The reset is the whole safety property and most of these tests are
about it.

The other one worth pinning is the error case. A board that fails to
respond is not a board with nothing new, and treating a transient outage
as silence would push a broken board toward the ceiling exactly when it
needs watching.

Run directly, no framework:  python tests/test_scrape_state.py
"""

import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

import scrape_state  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


T0 = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def entries(*domains):
    return [{"domain": d, "ats": "greenhouse", "token": d.split(".")[0]} for d in domains]


def result(domain, unchanged=False, error=None, etag=None):
    return {"domain": domain, "ats": None if error else "greenhouse",
            "unchanged": unchanged, "error": error, "etag": etag}


class FakeS3:
    def __init__(self, fail_put=False):
        self.objects = {}
        self.etags = {}
        self.fail_put = fail_put
        self.puts = 0

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        body = self.objects[Key]
        return {"Body": type("B", (), {"read": lambda _s, b=body: b})(),
                "ETag": self.etags[Key]}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts += 1
        if self.fail_put:
            raise RuntimeError("S3 said no")
        if "IfMatch" in kw and self.etags.get(Key) != kw["IfMatch"]:
            raise RuntimeError("PreconditionFailed")
        if "IfNoneMatch" in kw and Key in self.objects:
            raise RuntimeError("PreconditionFailed")
        self.objects[Key] = Body
        self.etags[Key] = '"v%d"' % self.puts


# An unchanged poll backs off; a change slams it back to the floor.
state = {}
scrape_state.record(state, [result("quiet.com", unchanged=True)], T0)
first = state["quiet.com"]["interval_s"]
check("a first quiet poll starts from the floor",
      first == round(scrape_state.FLOOR_S * scrape_state.GROWTH), str(first))

for i in range(1, 12):
    scrape_state.record(state, [result("quiet.com", unchanged=True)],
                        T0 + timedelta(minutes=i))
check("repeated silence reaches the ceiling and stops there",
      state["quiet.com"]["interval_s"] == scrape_state.CEILING_S,
      str(state["quiet.com"]["interval_s"]))

# The safety property. A board at the ceiling that posts is hot again.
scrape_state.record(state, [result("quiet.com", etag='"abc"')], T0 + timedelta(hours=1))
check("one change resets a ceilinged board to the floor",
      state["quiet.com"]["interval_s"] == scrape_state.FLOOR_S,
      str(state["quiet.com"]["interval_s"]))
check("and the change is what stores a validator",
      state["quiet.com"].get("etag") == '"abc"', str(state["quiet.com"]))

# An error is not silence.
errored = {"broken.com": {"interval_s": 300, "next_at": T0.isoformat()}}
scrape_state.record(errored, [result("broken.com", error="timeout")], T0)
check("a failing board holds its interval rather than backing off",
      errored["broken.com"]["interval_s"] == 300, str(errored["broken.com"]))

# A 304 carries back the validator it was given, so writing on unchanged
# would store what is already there.
keep = {"same.com": {"etag": '"original"', "interval_s": 180}}
scrape_state.record(keep, [result("same.com", unchanged=True, etag='"original"')], T0)
check("an unchanged poll leaves the stored validator alone",
      keep["same.com"]["etag"] == '"original"', str(keep["same.com"]))

# Selection.
state = {}
scrape_state.record(state, [result("a.com", unchanged=True)], T0)
due = scrape_state.due(state, entries("a.com"), T0 + timedelta(seconds=30))
check("a board inside its interval is skipped", due == [], str(due))
due = scrape_state.due(state, entries("a.com"), T0 + timedelta(hours=1))
check("and polled once the interval has passed", len(due) == 1, str(due))

# A newly discovered company must not wait out anyone else's backoff.
due = scrape_state.due(state, entries("a.com", "brand-new.com"), T0 + timedelta(seconds=30))
check("a board with no state is always due",
      [d["domain"] for d in due] == ["brand-new.com"], str(due))

# Validators have to reach the prober, or every poll is a full fetch.
state = {"v.com": {"etag": '"e1"', "content_hash": "h1",
                   "next_at": T0.isoformat(), "interval_s": 180}}
due = scrape_state.due(state, entries("v.com"), T0 + timedelta(minutes=5))
check("a due board carries its validators through",
      due and due[0].get("etag") == '"e1"' and due[0].get("content_hash") == "h1", str(due))
check("and keeps the fields the prober needs to fetch at all",
      due and due[0].get("token") == "v", str(due))

# Jitter, so boards that fall quiet together do not come due together.
# This is the one production corrected within ten minutes: the migration
# sweep polled every board at the same instant, so the whole quiet
# cohort shared a phase, and at 10% of a 270s interval the spread was
# narrower than the 5-minute tick. Sweeps went 8, then 202, then 3,238.
herd = {}
scrape_state.record(herd, [result("h%d.com" % i, unchanged=True) for i in range(2000)], T0)
for _ in range(6):  # drive them all to the ceiling together
    scrape_state.record(herd, [result(d, unchanged=True) for d in herd], T0)
spread = [scrape_state._parse(r["next_at"]) for r in herd.values()]
window = (max(spread) - min(spread)).total_seconds()
check("a cohort at the ceiling is spread wider than one schedule tick",
      window > 300, "%.0fs window" % window)
check("and never scheduled beyond the ceiling itself",
      max(spread) <= T0 + timedelta(seconds=scrape_state.CEILING_S), str(max(spread)))

# The hard cap, which makes a herd harmless even when jitter does not
# prevent one. A full sweep has already blown probe.py's 200s timeout.
many = {}
scrape_state.record(many, [result("m%d.com" % i, unchanged=True) for i in range(3400)], T0)
for r in many.values():
    r["next_at"] = (T0 - timedelta(hours=1)).isoformat()  # everything overdue at once
due_now = scrape_state.due(many, entries(*many.keys()), T0)
check("a sweep is capped however many boards are due",
      len(due_now) == scrape_state.MAX_PER_SWEEP, str(len(due_now)))

# Deferring has to be fair, or the same boards get skipped forever.
staggered = {}
for i in range(2000):
    staggered["s%d.com" % i] = {
        "interval_s": 1200,
        "next_at": (T0 - timedelta(seconds=i)).isoformat(),  # s1999 is most overdue
    }
picked = {e["domain"] for e in scrape_state.due(staggered, entries(*staggered.keys()), T0)}
check("the most overdue boards go first",
      "s1999.com" in picked and "s0.com" not in picked, str(len(picked)))

# A brand-new company must not be starved behind a backlog.
backlog = dict(staggered)
due_now = scrape_state.due(backlog, entries(*backlog.keys(), "fresh.com"), T0)
check("a never-seen board is swept even when the cap binds",
      "fresh.com" in {e["domain"] for e in due_now}, str(len(due_now)))

# Round trip through S3, gzipped, with the conditional write.
s3 = FakeS3()
state = {}
scrape_state.record(state, [result("r.com", unchanged=True)], T0)
check("a first save writes when the object does not exist",
      scrape_state.save("b", s3, state, None) is True)
loaded, etag = scrape_state.load("b", s3)
check("what comes back is what went in", loaded == state, str(loaded))
check("and it carries an etag to condition the next write on", bool(etag), str(etag))
check("a save with the current etag succeeds",
      scrape_state.save("b", s3, loaded, etag) is True)
check("a save with a stale etag loses the race rather than clobbering",
      scrape_state.save("b", s3, loaded, '"stale"') is False)

# The object is compressed: 3,400 boards is the real size and it is read
# once per sweep.
big = {}
scrape_state.record(big, [result("d%d.com" % i, unchanged=True) for i in range(3400)], T0)
raw = json.dumps(big).encode()
packed = gzip.compress(json.dumps(big, separators=(",", ":")).encode())
check("3,400 boards compress to well under a megabyte",
      len(packed) < 1_000_000, "%d bytes" % len(packed))
print("      (%d boards: %.0fKB raw, %.0fKB gzipped)" % (len(big), len(raw) / 1024, len(packed) / 1024))

# Failures never raise into a sweep.
check("an unwritable bucket returns False rather than raising",
      scrape_state.save("b", FakeS3(fail_put=True), {}, None) is False)
check("a missing object with no fallback loads empty",
      scrape_state.load("b", FakeS3()) == ({}, None))
check("and no bucket at all is a no-op",
      scrape_state.load("", FakeS3()) == ({}, None))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
