"""Delta fragments are chunked by the producer, so the applier's memory
is a property of a constant rather than of how busy a sweep was.

A fragment used to be "whatever one sweep found". That made its size a
function of the weather: 13MB on a quiet cycle, 50MB on a normal one,
about 100MB on 2026-09-11 when the sweep returned from an hour of
downtime and re-verified everything at once. The applier parses a
fragment whole, so that variance landed on its 2GB ceiling: it OOM'd
repeatedly, and a failed apply deletes nothing, so every retry faced a
bigger backlog than the last and was guaranteed to fail harder.

The case that matters most below is the skewed one. Companies alone is a
poor proxy for bytes, and it was that skew that produced a 50MB fragment
from only 57 companies.

Run directly, no framework:  python tests/test_delta_chunking.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

import deltas  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def company(domain, jobs=1, desc_bytes=100):
    return {
        "domain": domain, "ats": "greenhouse", "token": domain.split(".")[0],
        "jobs": [{"external_id": str(i), "title": "Engineer",
                  "description": "x" * desc_bytes} for i in range(jobs)],
    }


def sizes(payload):
    return [len(b"[" + b",".join(batch) + b"]") for batch in deltas._chunks(payload)]


def counts(payload):
    return [len(batch) for batch in deltas._chunks(payload)]


# Small companies: the company cap is what bites.
small = [company(f"c{i}.com") for i in range(60)]
check("a long run of small companies splits on the company cap",
      counts(small) == [25, 25, 10], repr(counts(small)))
check("and every chunk stays under the byte budget",
      all(n <= deltas.MAX_FRAGMENT_BYTES for n in sizes(small)), repr(sizes(small)))

# The real shape: a few companies carrying most of the weight. This is
# the 57-companies-and-50MB case.
heavy = [company(f"h{i}.com", jobs=400, desc_bytes=8000) for i in range(8)]
check("a few heavy companies split on bytes, not on count",
      len(counts(heavy)) > 1 and max(counts(heavy)) < deltas.MAX_FRAGMENT_COMPANIES,
      repr(counts(heavy)))
check("and no chunk exceeds the budget",
      all(n <= deltas.MAX_FRAGMENT_BYTES for n in sizes(heavy)), repr(sizes(heavy)))

# Mixed, which is what a real sweep looks like.
mixed = []
for i in range(40):
    mixed.append(company(f"m{i}.com", jobs=400 if i % 10 == 0 else 2, desc_bytes=4000))
check("a mixed sweep never produces an oversized chunk",
      all(n <= deltas.MAX_FRAGMENT_BYTES for n in sizes(mixed)), repr(sizes(mixed)))
check("and loses nobody along the way",
      sum(counts(mixed)) == len(mixed), f"{sum(counts(mixed))} of {len(mixed)}")

# One company bigger than the whole budget cannot be split without
# breaking the upsert's per-company shape, so it goes out alone.
giant = [company("giant.com", jobs=5000, desc_bytes=4000), company("after.com")]
first = next(iter(deltas._chunks(giant)))
check("a single company over budget still goes out, on its own",
      len(first) == 1, repr(counts(giant)))
check("and does not drag the next company with it",
      counts(giant) == [1, 1], repr(counts(giant)))

check("an empty payload produces no chunks", counts([]) == [])

# Order has to survive: keys sort lexicographically and the applier
# replays them in that order, relying on a later entry for the same
# company landing on top of an earlier one.
ordered = [company(f"o{i:03d}.com") for i in range(60)]
flat = [json.loads(b)["domain"] for batch in deltas._chunks(ordered) for b in batch]
check("chunking preserves company order",
      flat == [c["domain"] for c in ordered], repr(flat[:4]))

# Every chunk has to be valid JSON on its own: the applier json.loads
# each fragment body whole.
one = next(iter(deltas._chunks(small)))
body = b"[" + b",".join(one) + b"]"
parsed = json.loads(body)
check("a chunk's body parses as a JSON array of companies",
      isinstance(parsed, list) and parsed[0]["domain"] == "c0.com", repr(body[:40]))


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        self.puts.append((Key, len(Body)))


fake = FakeS3()
deltas.boto3 = type("m", (), {"client": staticmethod(lambda *a, **k: fake)})()
keys = deltas.put_fragment("bucket", small)
check("put_fragment writes one object per chunk",
      len(keys) == len(fake.puts) == 3, f"{len(keys)} keys, {len(fake.puts)} puts")
check("the keys sort into the order they were produced",
      keys == sorted(keys), repr(keys))
check("unchanged companies are still filtered out",
      deltas.put_fragment("bucket", [{"domain": "x.com", "ats": "greenhouse", "unchanged": True}]) == [])
check("and no bucket means no writes", deltas.put_fragment("", small) == [])

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
