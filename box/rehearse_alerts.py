"""Run the alert evaluator against the box, without sending or writing.

evaluate_alerts has no dry-run of its own, and a naive rehearsal does
two harmful things. It sends every digest a second time, to real people
who already got one from the Lambda. Worse, it advances
last_notified_at in DynamoDB, and that cursor is shared: moving it here
makes the LIVE Lambda skip those listings on its next run, so a
rehearsal meant to prove nothing breaks would quietly break alerts for
everyone.

So both are stubbed. What still runs for real is everything worth
proving: the DynamoDB scan (so the instance role's permissions are
tested), build_jobs_where against this box's own snapshot and its
capabilities, the match query per alert, and rendering the digest text
and HTML. What is faked is the last two inches, the SES call and the
cursor write.

    python box/rehearse_alerts.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "api", ROOT / "loader"):
    sys.path.insert(0, str(_p))

import alerts  # noqa: E402

DB = Path(os.environ.get("DATA_PATH", "/var/lib/otj/jobs.db"))

sent: list[tuple[str, int]] = []
cursor_writes: list[str] = []


def fake_send(alert, matches):
    # Render for real, so a template that would raise still raises here.
    n = len(matches)
    text = alerts._digest_text(n, matches, alert)
    html = alerts._digest_html(n, matches, alert)
    sent.append((alert.get("email") or alert.get("user_id", "?"), n))
    print(f"  WOULD SEND to {alert.get('email', alert.get('user_id'))}: "
          f"{n} matches, {len(text)} chars text, {len(html)} chars html")
    for m in matches[:3]:
        print(f"    - {m.get('title')} @ {m.get('company_domain')}")


class FakeTable:
    """Reads pass through, writes are counted and dropped."""

    def __init__(self, real):
        self._real = real

    def update_item(self, **kw):
        cursor_writes.append(str(kw.get("Key")))

    def __getattr__(self, name):
        return getattr(self._real, name)


real_table = alerts._dynamodb.Table


def fake_dynamo_table(name):
    return FakeTable(real_table(name))


alerts._send_digest = fake_send
alerts._dynamodb.Table = fake_dynamo_table

print(f"rehearsing against {DB} (nothing is sent, nothing is written)")
result = alerts.evaluate_alerts(DB)
print()
print(f"alerts checked:   {result.get('alerts_checked')}")
print(f"digests it would have sent: {len(sent)}")
print(f"cursor writes suppressed:   {len(cursor_writes)}")
print(f"watched domains:  {len(result.get('watched_domains') or [])}")
if result.get("errors"):
    print(f"ERRORS: {result['errors']}")
    sys.exit(1)
if result.get("skipped"):
    print(f"SKIPPED: {result['skipped']}")
    sys.exit(1)
print("ok")
