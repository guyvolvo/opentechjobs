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
