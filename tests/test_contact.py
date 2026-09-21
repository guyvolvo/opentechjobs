"""/api/contact: the contact page's form, sent on as one email.

Run directly, no framework:  python tests/test_contact.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
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


class FakeSes:
    def __init__(self):
        self.sent = []

    def send_email(self, **kw):
        self.sent.append(kw)


ses = FakeSes()
handler._ses_client = ses

status, body = handler.route_contact({"name": "Dana", "email": "dana@example.com", "message": "Hello, I run a board too. Can we talk about your API?"})
check("a real message is sent once, from the alerts sender, to the contact address, with the sender as reply-to",
      status == 200 and len(ses.sent) == 1 and ses.sent[0]["FromEmailAddress"] == handler.CONTACT_FROM
      and ses.sent[0]["Destination"] == {"ToAddresses": [handler.CONTACT_TO]} and ses.sent[0]["ReplyToAddresses"] == ["dana@example.com"], repr((status, body)))
mail = ses.sent[0]["Content"]["Simple"]
check("the subject names the sender and the body carries name, address and message",
      mail["Subject"]["Data"] == "opentechjobs.org contact: Dana" and "Dana <dana@example.com>" in mail["Body"]["Text"]["Data"]
      and "run a board too" in mail["Body"]["Text"]["Data"])

status, body = handler.route_contact({"email": "x", "message": "long enough message here"})
check("a malformed address is refused with the reason", status == 400 and "email" in body["error"] and len(ses.sent) == 1)
status, body = handler.route_contact({"email": "a@b.co", "message": "hi"})
check("a message too short to mean anything is refused", status == 400 and "short" in body["error"] and len(ses.sent) == 1)
status, body = handler.route_contact({"email": "a@b.co", "message": "x" * 5001})
check("a message past 5,000 characters is refused", status == 400 and len(ses.sent) == 1)
status, body = handler.route_contact({"email": "bot@spam.example", "message": "buy things now please", "website": "http://spam"})
check("the honeypot swallows a bot quietly: 200 and nothing sent", status == 200 and len(ses.sent) == 1)

r = handler.lambda_handler({"requestContext": {"http": {"method": "GET"}}, "rawPath": "/api/contact", "rawQueryString": ""}, None)
check("GET on the route is 405", r["statusCode"] == 405)
r = handler.lambda_handler({"requestContext": {"http": {"method": "POST"}}, "rawPath": "/api/contact", "rawQueryString": "",
                            "body": '{"email": "dana@example.com", "message": "Hello from the form, this is a test."}'}, None)
check("POST through the handler reaches the route", r["statusCode"] == 200 and len(ses.sent) == 2, repr(r["body"]))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
