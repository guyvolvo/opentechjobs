"""Alert evaluation: matches each active alert's filter against jobs
first-seen since its own watermark, sends one digest email per alert
with any new matches, advances the watermark. Called once per fast-poll
cycle (scrape_handler.py, right after the loader step, while jobs.db is
already fresh on /tmp -- no separate download needed here).

Uses job_filters.build_jobs_where() for the actual matching, the same
function /api/jobs itself uses (api/handler.py) -- an alert matches
exactly what its owner would see applying those filters on the live
board, not a second, independently-drifting approximation of it.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Attr

from job_filters import build_jobs_where

ALERTS_TABLE = os.environ.get("ALERTS_TABLE")
FROM_EMAIL = os.environ.get("ALERTS_FROM_EMAIL", "alerts@guyvoloshin.com")
SITE_ORIGIN = os.environ.get("SITE_ORIGIN", "https://opentechjobs.org")

_dynamodb = boto3.resource("dynamodb")
_ses = boto3.client("sesv2")


def evaluate_alerts(jobs_db_path: Path) -> dict:
    if not ALERTS_TABLE:
        # Not every environment running scrape_handler.py needs this
        # (local testing, a future non-alerts deployment) -- absence
        # means "don't evaluate," not an error.
        return {"skipped": "ALERTS_TABLE not set"}

    table = _dynamodb.Table(ALERTS_TABLE)
    alerts = _scan_active_alerts(table)

    # Read-only, and the loader step just finished writing this same
    # file moments ago in the same invocation -- no reason to hold a
    # write lock or risk racing it.
    conn = sqlite3.connect(f"file:{jobs_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    sent = 0
    errors = []
    for alert in alerts:
        try:
            matches = _find_new_matches(conn, alert)
            if matches:
                _send_digest(alert, matches)
                sent += 1
            table.update_item(
                Key={"user_id": alert["user_id"], "alert_id": alert["alert_id"]},
                UpdateExpression="SET last_notified_at = :t",
                ExpressionAttributeValues={":t": datetime.now(timezone.utc).isoformat()},
            )
        except Exception as e:
            # One user's bad filter or bounced address shouldn't stop
            # every other alert from being checked.
            errors.append(f"{alert['user_id']}/{alert['alert_id']}: {e}")

    conn.close()
    return {"alerts_checked": len(alerts), "digests_sent": sent, "errors": errors}


def _scan_active_alerts(table) -> list[dict]:
    # No GSI on active (see infra/dynamodb.tf) -- full scan, filtered
    # client-side, cheap at this project's expected alert volume.
    items = []
    resp = table.scan(FilterExpression=Attr("active").eq(True))
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(FilterExpression=Attr("active").eq(True), ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


def _find_new_matches(conn: sqlite3.Connection, alert: dict) -> list[dict]:
    filter_params = dict(alert.get("filter") or {})
    where_sql, args = build_jobs_where(filter_params)
    # Always present: route_create_alert (api/handler.py) sets this to
    # created_at at creation time specifically so a brand-new alert's
    # first evaluation only picks up genuinely new postings, not every
    # already-open job that happened to match on day one.
    watermark = alert["last_notified_at"]
    where_sql += " AND first_seen > ?"
    args = [*args, watermark]

    rows = conn.execute(
        f"SELECT id, title, company_domain, location, url FROM jobs "
        f"WHERE {where_sql} ORDER BY first_seen DESC LIMIT 50",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def _send_digest(alert: dict, matches: list[dict]) -> None:
    to_email = alert.get("email")
    if not to_email:
        return
    n = len(matches)
    lines = [f"{n} new listing{'s' if n != 1 else ''} match your OpenTechJobs alert:", ""]
    for j in matches:
        lines.append(f"- {j['title']} — {j['company_domain']} ({j['location'] or 'location unknown'})")
        lines.append(f"  {j['url']}")
    lines.append("")
    lines.append(f"Manage this alert: {SITE_ORIGIN}/")
    body = "\n".join(lines)

    _ses.send_email(
        FromEmailAddress=FROM_EMAIL,
        Destination={"ToAddresses": [to_email]},
        Content={
            "Simple": {
                "Subject": {"Data": f"{n} new job{'s' if n != 1 else ''} on OpenTechJobs"},
                "Body": {"Text": {"Data": body}},
            }
        },
    )
