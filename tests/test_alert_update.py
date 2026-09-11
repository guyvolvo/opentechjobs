"""PATCH /me/alerts/{id}: pause, edit the filter, or both.

Editing the filter is new. It used to be delete-and-recreate, which lost
the alert_id and the created_at every time someone changed their mind
about one word in a search.

Two things in the update expression are easy to get wrong and only fail
against the real table: `filter` is a DynamoDB reserved word, so it
works only behind a name placeholder, and passing an empty
ExpressionAttributeNames map is itself an error. Both are pinned below.

Run directly, no framework:  python tests/test_alert_update.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

# handler.py builds its DynamoDB resource at import time. No API call is
# made from these tests, but botocore still walks the credential chain
# while constructing the client, so hand it throwaway keys rather than
# letting it reach for the machine's real profile.
os.environ.setdefault("ALERTS_TABLE", "test-alerts")
os.environ.setdefault("DATA_BUCKET", "test-bucket")
os.environ.setdefault("DATA_KEY", "jobs.db")
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import handler  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def args(body):
    return handler._alert_update_args("user-1", "alert-1", body)


def rejects(body):
    try:
        args(body)
    except ValueError:
        return True
    return False


a = args({"active": False})
check("a pause sends no names map",
      a["UpdateExpression"] == "SET active = :a" and "ExpressionAttributeNames" not in a,
      repr(a))
check("and carries the new value", a["ExpressionAttributeValues"] == {":a": False})

a = args({"filter": {"q": "devops"}})
check("a filter edit hides the reserved word behind a placeholder",
      "#f = :f" in a["UpdateExpression"] and a["ExpressionAttributeNames"] == {"#f": "filter"},
      repr(a))
check("and writes the filter through", a["ExpressionAttributeValues"][":f"] == {"q": "devops"})

# Widening an alert must not mail out every already-open job the new
# filter happens to match, so the watermark moves with the edit.
check("a filter edit moves last_notified_at forward",
      "last_notified_at = :n" in a["UpdateExpression"] and bool(a["ExpressionAttributeValues"][":n"]))

a = args({"active": True, "filter": {"q": "sre"}})
check("both at once is one update",
      a["UpdateExpression"].startswith("SET ")
      and "active = :a" in a["UpdateExpression"]
      and "#f = :f" in a["UpdateExpression"]
      and a["UpdateExpression"].count(",") == 2,
      a["UpdateExpression"])

a = args({"active": True})
check("the row is scoped to its owner",
      a["Key"] == {"user_id": "user-1", "alert_id": "alert-1"}
      and a["ConditionExpression"] == "attribute_exists(alert_id)")

check("an empty body is rejected", rejects({}))
check("an unknown filter key is rejected", rejects({"filter": {"nonsense": "1"}}))
check("a filter that is not an object is rejected", rejects({"filter": "q=devops"}))
check("every key /api/jobs accepts is allowed",
      all(args({"filter": {k: "x"}}) for k in handler._ALLOWED_FILTER_KEYS))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
