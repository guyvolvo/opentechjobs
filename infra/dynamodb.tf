# Alerts only -- everything else (jobs.db) stays exactly as it is:
# read-only, unauthenticated, batch-upserted by the scraper. This is the
# one piece of real per-user mutable state the product needed a login
# system for at all.
#
# On-demand (PAY_PER_REQUEST), not provisioned: at this project's scale
# the "Always Free" 25 RCU/WCU provisioned tier would also stay $0, but
# on-demand needs no capacity planning at all and this table's traffic is
# inherently bursty (a user's own CRUD calls, plus one Scan per fast-poll
# cycle from the alert evaluator) rather than steady.

resource "aws_dynamodb_table" "alerts" {
  name         = "${var.project_name}-alerts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "alert_id"

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "alert_id"
    type = "S"
  }

  # No GSI on `active`: the evaluator Lambda does a full table Scan,
  # filtering active=true client-side, once per fast-poll cycle. Cheap at
  # this project's expected scale (a handful to low hundreds of alerts);
  # revisit with a GSI only if that stops being true.
}

# Conditional-poll validators, one row per company. Exists so the
# fast-poll can ask "has this board changed?" without downloading the
# 30-90MB partition file that would otherwise be the only place to keep
# an ETag -- which defeats the point, since avoiding that download is
# most of the saving.
#
# known.json can't hold these either: export_known() rewrites it
# wholesale from the discover DB and merge_discovered_batch.py upserts
# into it separately, so a validator column there gets clobbered by
# whichever writes last.
#
# Tiny and hot: one BatchGetItem of <=50 keys per fast-poll invocation,
# and writes only for companies that actually moved. Same PAY_PER_REQUEST
# reasoning as the alerts table above.
resource "aws_dynamodb_table" "scrape_state" {
  name         = "${var.project_name}-scrape-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "domain"

  attribute {
    name = "domain"
    type = "S"
  }

  # No TTL. A stale validator is self-correcting: the board answers 200
  # instead of 304 and the row is overwritten on the spot. Expiring rows
  # would only ever throw away a working ETag and force a full fetch.
}
