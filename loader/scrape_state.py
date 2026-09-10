"""Per-board poll scheduling and conditional-GET validators, in one S3 object.

Two jobs that used to be one, plus one that did not exist.

The validators (etag, last_modified, content_hash) are what make a sweep
cheap: hand a board the one it gave us last time and about 88% answer
304 with no body. Those lived in DynamoDB, read 3,434 at a time every
five minutes, which is 286,000 read units a day to fetch roughly 200KB
of text that mostly never changes.

The new job is deciding WHICH boards to poll. Measured over 27
consecutive sweeps, 101 of 3,434 boards changed at all, and 60 of those
changed exactly once. The other 97% answered "nothing changed" 27 times
in a row, and each of those answers still costs a round trip inside a
function billed by the millisecond.

So each board carries its own interval. A detected change resets it to
FLOOR, which is faster than the flat five minutes every board gets
today. An unchanged poll multiplies it by GROWTH, up to CEILING.
Nothing is tiered by calendar age: this corpus is nine days old, so any
threshold in days would put almost every board in one bucket while
saying nothing about how often it actually moves.

The trade, stated plainly: a board that has never changed is polled
every 20 minutes rather than every 5, so its first new listing can be up
to 20 minutes late. The moment that listing lands, the board is back to
3-minute polling. Boards that move get watched more closely than they
are today; boards that never move get watched less.
"""

import gzip
import json
import random
import sys
from datetime import datetime, timedelta, timezone

KEY = "scrape-state.json.gz"

# A change resets to this. Faster than the flat 5 minutes every board
# gets today, which is the point: the saving buys better freshness where
# freshness is worth something.
FLOOR_S = 180

# Nothing waits longer than this, however long it has been quiet.
CEILING_S = 1200

# Gentle on purpose. At 1.5 a board reaches the ceiling after five
# consecutive quiet polls, roughly half an hour of silence, so a board
# posting a few times a day never drifts far from the floor.
GROWTH = 1.5

# Without jitter, boards that fall quiet together stay in lockstep and
# come due in one lump.
#
# Ten percent was not enough and production said so within ten minutes.
# The first sweep after migration polled all 3,434 boards at the same
# instant, so the entire quiet cohort shared a phase; at 10% of a 270s
# interval the spread is +/-27s, far narrower than the 5-minute tick, so
# they all landed in the same sweep anyway. Observed: 8 boards, then
# 202, then 3,238.
#
# 35%, and applied downward only. Symmetric jitter would push a board at
# the ceiling out to 27 minutes, quietly breaking the one guarantee this
# module makes: nothing waits longer than CEILING_S. Subtracting instead
# spreads a cohort over the 7 minutes below the ceiling, which is still
# wider than the tick, while the worst case stays exactly 20 minutes.
JITTER = 0.35

# A hard ceiling on one sweep, whatever the schedule thinks is due.
#
# Jitter makes a herd unlikely; this makes it harmless. probe.py runs
# under a 200-second subprocess timeout and a full 3,434-board sweep has
# already blown it once, under contention during the migration. Deferred
# boards are not dropped: they stay overdue and go to the front of the
# queue on the next tick, which is 5 minutes away.
#
# 1,200 is roughly 25 seconds of sweeping, against that 200-second
# ceiling. Generous margin, and still well above the ~850 a converged
# steady state should actually ask for.
MAX_PER_SWEEP = 1200

_VALIDATORS = ("etag", "last_modified", "content_hash")


def _now():
    return datetime.now(timezone.utc)


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def load(bucket, s3, dynamo_table=""):
    """(state, etag). Empty state on anything unreadable.

    dynamo_table is a migration path and nothing more. The first run
    after this ships finds no S3 object, and starting from nothing would
    discard every validator at once and force 3,434 full fetches in one
    invocation, which is the shape that used to blow probe.py's
    subprocess timeout. Seeding from the old table makes the switch
    free. It can go once the object exists.
    """
    if not bucket:
        return {}, None
    try:
        obj = s3.get_object(Bucket=bucket, Key=KEY)
        return json.loads(gzip.decompress(obj["Body"].read())), obj["ETag"]
    except Exception:
        pass
    if dynamo_table:
        seeded = _seed_from_dynamo(dynamo_table)
        if seeded:
            print("seeded poll state from %s: %d boards" % (dynamo_table, len(seeded)),
                  file=sys.stderr)
            return seeded, None
    return {}, None


def _seed_from_dynamo(table):
    try:
        import boto3

        client = boto3.client("dynamodb")
        out = {}
        kw = {
            "TableName": table,
            "ProjectionExpression": "#d, etag, last_modified, content_hash",
            "ExpressionAttributeNames": {"#d": "domain"},
        }
        while True:
            resp = client.scan(**kw)
            for item in resp.get("Items", []):
                out[item["domain"]["S"]] = {
                    k: item[k]["S"] for k in _VALIDATORS if k in item
                }
            if "LastEvaluatedKey" not in resp:
                return out
            kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:
        print("couldn't seed from %s (non-fatal, boards just refetch): %r" % (table, e),
              file=sys.stderr)
        return {}


def save(bucket, s3, state, etag):
    """Conditional write. False means another sweep got there first, in
    which case this run's scheduling is lost and the next one recomputes
    it. Never raises: losing this costs full fetches, which is a cost
    problem rather than a data one.
    """
    if not bucket:
        return False
    body = gzip.compress(json.dumps(state, separators=(",", ":")).encode("utf-8"))
    kwargs = {
        "Bucket": bucket,
        "Key": KEY,
        "Body": body,
        "ContentType": "application/json",
        "ContentEncoding": "gzip",
    }
    if etag:
        kwargs["IfMatch"] = etag
    else:
        kwargs["IfNoneMatch"] = "*"
    try:
        s3.put_object(**kwargs)
        return True
    except Exception as e:
        print("couldn't save poll state (non-fatal): %r" % (e,), file=sys.stderr)
        return False


def due(state, entries, now=None):
    """The boards worth polling this sweep, each carrying its validators.

    A board with no state has never been seen and is always due. That is
    how a newly discovered company gets picked up on the next tick
    instead of waiting out someone else's backoff.
    """
    now = now or _now()
    scored = []
    for e in entries:
        row = state.get(e.get("domain", ""))
        if row is None:
            # Never seen. Sorts first so a newly discovered company is
            # never starved by a backlog of merely-overdue boards.
            scored.append((float("-inf"), dict(e)))
            continue
        nxt = _parse(row.get("next_at"))
        if nxt is not None and nxt > now:
            continue
        merged = dict(e)
        merged.update({k: row[k] for k in _VALIDATORS if k in row})
        scored.append((nxt.timestamp() if nxt else float("-inf"), merged))

    if len(scored) <= MAX_PER_SWEEP:
        return [e for _, e in scored]
    # Most overdue first, so deferring is fair rather than arbitrary and
    # nothing can be starved indefinitely.
    scored.sort(key=lambda pair: pair[0])
    print("%d boards due, sweeping the %d most overdue" % (len(scored), MAX_PER_SWEEP),
          file=sys.stderr)
    return [e for _, e in scored[:MAX_PER_SWEEP]]


def record(state, results, now=None):
    """Fold one sweep's outcome back in. Returns a summary for logging.

    Three outcomes, scheduled differently.

    A change resets to the floor, because a board that just posted is
    the likeliest thing on the whole board to post again.

    An unchanged poll backs off. That is the saving.

    An error holds the interval where it is rather than growing it. A
    board that is failing is not a board that is quiet, and letting a
    transient outage push it toward the ceiling would mean its listings
    come back and nobody notices for twenty minutes.
    """
    now = now or _now()
    counts = {"changed": 0, "unchanged": 0, "errored": 0}
    for r in results:
        domain = r.get("domain")
        if not domain:
            continue
        row = dict(state.get(domain) or {})
        if r.get("error") or not r.get("ats"):
            counts["errored"] += 1
            interval = float(row.get("interval_s") or FLOOR_S)
        elif r.get("unchanged"):
            counts["unchanged"] += 1
            interval = min(float(row.get("interval_s") or FLOOR_S) * GROWTH, CEILING_S)
        else:
            counts["changed"] += 1
            interval = float(FLOOR_S)
            row["last_change"] = now.isoformat()
            # Only a real fetch learns a new validator. A 304 hands back
            # the one it was given, so writing on unchanged would store
            # what is already there.
            for k in _VALIDATORS:
                if r.get(k):
                    row[k] = str(r[k])
        row["interval_s"] = round(interval)
        spread = interval * random.uniform(-JITTER, 0)
        row["next_at"] = (now + timedelta(seconds=interval + spread)).isoformat()
        state[domain] = row
    return counts
