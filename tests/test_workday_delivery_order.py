"""Deliver the jobs first, then remember having read the board.

The live incident, counted 2026-09-22 against /api/companies?ats=workday:
1,212 of the 2,800 Workday tenants had ever landed a job in jobs-read.db.
The other 1,614 held about 372,000 jobs and were unreachable.

lambda_handler wrote each tenant's fingerprint into the poll state and
pushed that state to S3 BEFORE put_fragment sent anything, with the
loader subprocess in between raising on a non-zero exit. Any run that
died in that window committed "I already read this tenant" for jobs that
never left the Lambda. The next run compared the stored fingerprint to a
board that had not moved, reported unchanged with no jobs, and deltas.py
drops unchanged results from fragments. A quiet board never escaped.

So the ordering is the fix, and the version is the recovery. The stranded
rows are in S3 and cannot be edited from here, so WORKDAY_STATE_VERSION
makes the handler distrust a fingerprint stored under older rules, read
each board once more, and settle back. Same trick as LOGO_CHECK_VERSION
in resolve_company_logos.py.

Run directly, no framework:  python tests/test_workday_delivery_order.py
"""

import gzip
import io
import json
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "loader"))
os.environ.setdefault("DATA_BUCKET", "test-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import probe  # noqa: E402
import scrape_state  # noqa: E402
import scrape_workday_handler as handler  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def posting(i):
    return {"title": f"Role {i}", "externalPath": f"/job/Tel-Aviv/Role-{i}_R{i:04d}",
            "locationsText": "Tel Aviv, Israel", "postedOn": "Posted 3 Days Ago",
            "bulletFields": [f"R{i:04d}"]}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return self._body


class Sess:
    """One tenant's board, counting every request so a test can say what
    a poll cost. Same shape as tests/test_workday_poll.py's."""

    def __init__(self, postings):
        self.postings = postings
        self.calls, self.lock = [], threading.Lock()
        self.headers = {}

    def post(self, url, json=None, **kw):
        with self.lock:
            self.calls.append(("POST", url, json.get("offset")))
        off = json.get("offset", 0)
        return Resp(200, {"total": len(self.postings) if off == 0 else 0,
                          "jobPostings": self.postings[off:off + json.get("limit", 20)],
                          "facets": []})

    def get(self, url, **kw):
        with self.lock:
            self.calls.append(("GET", url, None))
        return Resp(200, {"jobPostingInfo": {"jobDescription": "<p>Build things</p>",
                                             "location": "Tel Aviv, Israel"}})


POSTINGS = [posting(i) for i in range(45)]
ENTRY = {"domain": "acme.com", "tenant": "acme", "wd": "wd1", "site": "External"}
# Every id already in the partition and already described, which is what a
# stranded tenant looks like: read many times, delivered never.
KNOWN = {f"R{i:04d}" for i in range(45)}
FP = probe.workday_fingerprint(probe.workday_page1(Sess(POSTINGS), "acme", "wd1", "External"))

# The stranded row, exactly as the old code left it: a fingerprint, and
# no version because nothing wrote one.
state = {"acme.com": {"content_hash": FP, "interval_s": 14400}}
sess = Sess(POSTINGS)
stranded = handler._poll_workday(sess, ENTRY, state["acme.com"], KNOWN)
check("a fingerprint stored under older rules does not count as unchanged",
      not stranded.get("unchanged") and stranded["job_count"] == 45, repr(stranded)[:160])
check("the re-read is a page walk and nothing more, no description refetched",
      [c[0] for c in sess.calls] == ["POST", "POST", "POST"], repr(sess.calls)[:200])

scrape_state.record(state, [stranded], version=handler.WORKDAY_STATE_VERSION)
check("record stamps the current version on the row it just delivered",
      state["acme.com"].get("version") == handler.WORKDAY_STATE_VERSION
      and state["acme.com"]["content_hash"] == FP, repr(state["acme.com"]))

sess = Sess(POSTINGS)
settled = handler._poll_workday(sess, ENTRY, state["acme.com"], KNOWN)
check("once the version is current the fingerprint is trusted again, one request",
      settled.get("unchanged") is True and len(sess.calls) == 1, repr(sess.calls))

one_behind = {"content_hash": FP, "version": handler.WORKDAY_STATE_VERSION - 1}
check("a row one version behind is re-read too, not only a row with no version",
      not handler._poll_workday(Sess(POSTINGS), ENTRY, one_behind, KNOWN).get("unchanged"))
check("garbage in the version field is treated as old, not as current",
      not handler._delivered_under_current_rules({"version": "banana"})
      and not handler._delivered_under_current_rules(None))

# A tenant that failed this run must not get to claim the version. If it
# did, it would strand itself the same way, quietly, on the one run that
# was supposed to rescue it.
errored = {"gone.com": {"content_hash": "old", "version": handler.WORKDAY_STATE_VERSION - 1}}
scrape_state.record(errored, [{"domain": "gone.com", "ats": None, "error": "timeout"}],
                    version=handler.WORKDAY_STATE_VERSION)
check("a tenant that errored keeps its old version and stays due for a re-read",
      errored["gone.com"].get("version") == handler.WORKDAY_STATE_VERSION - 1)

# The fast sweep shares this module and passes no version. It must not
# start growing a field it never asked for.
plain = {}
scrape_state.record(plain, [{"domain": "wiz.io", "ats": "greenhouse", "content_hash": "abc"}])
check("a caller that passes no version writes none", "version" not in plain["wiz.io"])


# The handler end to end, with S3, the fragment write and the loader faked.
events = []
delivered = []


class FakeS3:
    def __init__(self, state_obj=None):
        self.state_obj = state_obj
        self.saved_state = None
        self.last_status = None

    def get_object(self, Bucket, Key):
        if Key == handler.WORKDAY_STATE_KEY and self.state_obj is not None:
            return {"Body": io.BytesIO(gzip.compress(json.dumps(self.state_obj).encode("utf-8"))),
                    "ETag": '"e1"'}
        raise RuntimeError("no object at " + Key)

    def put_object(self, **kw):
        if kw["Key"] == handler.WORKDAY_STATE_KEY:
            events.append("state")
            self.saved_state = json.loads(gzip.decompress(kw["Body"]))
        elif kw["Key"] == "status.json":
            self.last_status = json.loads(kw["Body"])


class FakeLoader:
    """Stands in for the load_to_sqlite subprocess."""

    def __init__(self, returncode=0, raises=None):
        self.returncode, self.raises = returncode, raises

    def run(self, *args, **kwargs):
        events.append("loader")
        if self.raises:
            raise self.raises
        return types.SimpleNamespace(returncode=self.returncode, stdout="", stderr="loader said so")


def fragment_writer(fail=None):
    def put_fragment(bucket, results):
        events.append("deliver")
        if fail:
            raise fail
        # Same filter deltas.put_fragment applies: an unchanged company
        # carries no jobs, so it is not written at all.
        sent = [r["domain"] for r in results if r.get("ats") and not r.get("unchanged")]
        delivered.extend(sent)
        return ["deltas/20260922T000000-000-abcd1234.json"] if sent else []
    return put_fragment


def run_once(state_obj, loader=None, put_fragment=None, sess=None):
    """One lambda_handler invocation against one fake tenant."""
    del events[:], delivered[:]
    s3 = FakeS3(state_obj)
    sess = sess or Sess(POSTINGS)
    saved = (handler.boto3, handler.subprocess, handler.put_fragment,
             handler._known_state_by_domain, handler._workday_entries, handler.TMP, probe.session)
    handler.boto3 = types.SimpleNamespace(client=lambda service: s3)
    handler.subprocess = loader or FakeLoader()
    handler.put_fragment = put_fragment or fragment_writer()
    handler._known_state_by_domain = lambda: ({"acme.com": KNOWN}, {"acme.com": KNOWN}, {})
    handler._workday_entries = lambda: [dict(ENTRY)]
    handler.TMP = Path(tempfile.mkdtemp())
    probe.session = lambda: sess
    try:
        return s3, handler.lambda_handler({"workday_only": True}, None)
    finally:
        (handler.boto3, handler.subprocess, handler.put_fragment,
         handler._known_state_by_domain, handler._workday_entries, handler.TMP,
         probe.session) = saved


s3, out = run_once({"acme.com": {"content_hash": FP, "interval_s": 14400}})
check("a stranded tenant is delivered on the first run after the version bump",
      delivered == ["acme.com"] and out["fragments"] == 1, repr((delivered, out)))
check("the fragment goes out before the poll state is committed",
      events.index("deliver") < events.index("state"), repr(events))
check("the loader runs after both, because it is a cache and nothing waits on it",
      events.index("loader") > events.index("state"), repr(events))
check("the saved row carries the current version and the fingerprint together",
      s3.saved_state["acme.com"]["version"] == handler.WORKDAY_STATE_VERSION
      and s3.saved_state["acme.com"]["content_hash"] == FP, repr(s3.saved_state))

# The second run, an hour later. Nothing has changed on the board, so the
# recovery has to stop costing anything.
settled_state = json.loads(json.dumps(s3.saved_state))
settled_state["acme.com"]["next_at"] = "2020-01-01T00:00:00+00:00"
sess = Sess(POSTINGS)
s3, out = run_once(settled_state, sess=sess)
check("the second run reads page 1, finds nothing new, and sends nothing",
      delivered == [] and out["fragments"] == 0 and len(sess.calls) == 1, repr((delivered, sess.calls)))

# A loader failure is a lost cache, not a lost delivery. This is the exit
# path that used to raise.
s3, out = run_once({"acme.com": {"content_hash": FP, "interval_s": 14400}},
                   loader=FakeLoader(returncode=3))
check("a loader that exits non-zero does not stop the jobs going out",
      delivered == ["acme.com"] and out["fragments"] == 1, repr((delivered, out)))
check("the exit code comes back in the return value instead of an exception",
      out["loader_error"] == "load_to_sqlite.py exited 3", repr(out))
check("the poll state is still saved, so the tenant is not re-read for nothing",
      out["state_saved"] is True and s3.saved_state["acme.com"]["version"] == handler.WORKDAY_STATE_VERSION)
check("and the run reports idle, because delivery is what the status is about",
      s3.last_status["phase"] == "idle" and "partition cache not written" in s3.last_status["detail"],
      repr(s3.last_status))

s3, out = run_once({"acme.com": {"content_hash": FP, "interval_s": 14400}},
                   loader=FakeLoader(raises=OSError("Cannot allocate memory")))
check("a loader that cannot even start is handled the same way",
      delivered == ["acme.com"] and "Cannot allocate memory" in out["loader_error"], repr(out))

# The other direction, which is the whole point. Delivery fails, so
# nothing may be remembered.
try:
    s3, out = run_once({"acme.com": {"content_hash": FP, "interval_s": 14400}},
                       put_fragment=fragment_writer(fail=RuntimeError("S3 said no")))
    raised = None
except RuntimeError as e:
    raised = e
check("a run that cannot deliver fails instead of committing the fingerprint",
      raised is not None and "state" not in events, repr(events))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
