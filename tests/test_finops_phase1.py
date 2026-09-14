"""The two cost changes that could lose data if they were wrong.

1. s3_pull reusing a local copy instead of downloading. The applier used to
   pull the whole snapshot every five minutes only to push it back. Reuse is
   only safe if a file that was changed and not pushed can never pass for a
   clean copy, so every way of getting that wrong is checked here.

2. every_hours on slow-board pins. A board read less often must still be read
   when it is due, and a board with no history must always be read.

Run directly, no framework:  python tests/test_finops_phase1.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

os.environ.setdefault("DATA_BUCKET", "test-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

import load_to_sqlite  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


class Body:
    def __init__(self, data):
        self.data = data

    def iter_chunks(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i:i + size]


class FakeS3:
    """One bucket in memory. ETags change with every write."""

    def __init__(self):
        self.objects = {}
        self.version = 0
        self.gets = 0

    def _put(self, key, data):
        self.version += 1
        self.objects[key] = (data, f'"v{self.version}"')
        return self.objects[key][1]

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ETag": self.objects[Key][1]}

    def get_object(self, Bucket, Key):
        self.gets += 1
        data, etag = self.objects[Key]
        return {"Body": Body(data), "ETag": etag}

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None):
        current = self.objects.get(Key)
        if IfMatch is not None and (current is None or current[1] != IfMatch):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        if IfNoneMatch == "*" and current is not None:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        return {"ETag": self._put(Key, Body.read())}


fake = FakeS3()
boto3.client = lambda *a, **k: fake

with tempfile.TemporaryDirectory() as td:
    local = Path(td) / "jobs-read.db"
    fake._put("jobs-read.db", b"snapshot-one")

    existed, etag = load_to_sqlite.s3_pull("b", "jobs-read.db", local)
    check("a first pull downloads", existed and fake.gets == 1 and local.read_bytes() == b"snapshot-one")

    local.write_bytes(b"snapshot-two")
    check("a successful conditional push", load_to_sqlite.s3_push_conditional("b", "jobs-read.db", local, etag))
    check("leaves a marker naming what S3 now holds",
          (Path(td) / "jobs-read.db.etag").read_text() == fake.objects["jobs-read.db"][1])

    existed, etag2 = load_to_sqlite.s3_pull("b", "jobs-read.db", local)
    check("the next pull reuses the file instead of downloading",
          fake.gets == 1 and etag2 == fake.objects["jobs-read.db"][1] and local.read_bytes() == b"snapshot-two",
          repr((fake.gets, etag2)))
    check("and removes the marker before handing the file over", not (Path(td) / "jobs-read.db.etag").exists())

    # Changed locally, never pushed (the run failed after the load).
    local.write_bytes(b"half-applied")
    load_to_sqlite.s3_pull("b", "jobs-read.db", local)
    check("a file changed but not pushed is never reused",
          fake.gets == 2 and local.read_bytes() == b"snapshot-two", repr((fake.gets, local.read_bytes())))

    # Pushed, then someone else wrote a newer version.
    local.write_bytes(b"snapshot-three")
    _, etag3 = load_to_sqlite.s3_pull("b", "jobs-read.db", local)
    local.write_bytes(b"snapshot-three")
    load_to_sqlite.s3_push_conditional("b", "jobs-read.db", local, etag3)
    fake._put("jobs-read.db", b"someone-elses")
    load_to_sqlite.s3_pull("b", "jobs-read.db", local)
    check("a newer version in S3 is downloaded even with a marker present",
          local.read_bytes() == b"someone-elses", repr(local.read_bytes()))

    # A conflicting push writes no marker.
    _, stale = "x", '"v-stale"'
    local.write_bytes(b"conflict")
    ok = load_to_sqlite.s3_push_conditional("b", "jobs-read.db", local, stale)
    check("a push that loses the race reports it", ok is False)
    check("and leaves no marker behind", not (Path(td) / "jobs-read.db.etag").exists())

    check("a missing object is still reported as missing",
          load_to_sqlite.s3_pull("b", "nothing-here.db", Path(td) / "nothing.db") == (False, None))

# every_hours.
import scrape_workday_handler as h  # noqa: E402

now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
ago = lambda hours: (now - timedelta(hours=hours)).isoformat()
check("no history is due", h._due({"every_hours": "4"}, None, now))
check("an hourly board polled an hour ago is due", h._due({}, ago(1), now))
check("an hourly board is due even when the schedule fires a little early", h._due({}, ago(0.9), now))
check("a four-hour board polled two hours ago is not due", not h._due({"every_hours": "4"}, ago(2), now))
check("and is due at four hours", h._due({"every_hours": "4"}, ago(4), now))
check("YAML's string value is read as a number", not h._due({"every_hours": "4"}, ago(3), now))
check("a bad value falls back to hourly", h._due({"every_hours": "soon"}, ago(1), now))
check("an unparseable timestamp is due", h._due({"every_hours": "4"}, "yesterday", now))
check("a naive timestamp is read as UTC", not h._due({"every_hours": "4"}, "2026-09-14T11:00:00", now))

import probe  # noqa: E402

pins = probe.load_pins()
check("the slow global boards are set to every four hours",
      all(pins.get(ats, {}).get(d, {}).get("every_hours") == "4"
          for ats, d in [("microsoft", "microsoft.com"), ("google", "google.com"),
                         ("apple", "apple.com"), ("amazon", "amazon.com")]),
      repr({d: pins.get(a, {}).get(d, {}).get("every_hours") for a, d in [("amazon", "amazon.com"), ("apple", "apple.com")]}))
check("AWS stays hourly", "every_hours" not in pins.get("amazon", {}).get("aws.amazon.com", {}))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
