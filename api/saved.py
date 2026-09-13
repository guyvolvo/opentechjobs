"""Saved ("starred") jobs, kept on the server instead of in the browser.

A star used to live only in the reader's own localStorage. Two things
were wrong with that, both reported live: a star did not follow anyone
to a second device, and the board's Saved view could only show the stars
that happened to be among the 50 rows already loaded, so a job starred
last week simply was not there.

One row per saved job, in the same DynamoDB table as the alerts and the
profile. Same partition as everything else belonging to a user, so a
delete-my-account is still one Query and one BatchWrite:

    {user_id, alert_id: "#saved#<job_id>", job_id, saved_at}

The sort key carries a sentinel prefix for the same reason profile.py's
single row does: an alert_id is a uuid4, which can never start with "#",
so a saved row cannot collide with one and the alert list can tell them
apart with a string test. The evaluator (alerts.py) scans with a filter
on `active`, which a saved row does not carry, so these stay invisible
there without it needing to know they exist.

The job itself is not copied here. Only the id is stored, and the board
reads the rows back through /api/jobs?ids=..., so a saved listing shows
its current title, its current salary, and whether it has since closed,
rather than a snapshot frozen at the moment someone clicked the star.
"""

# The same shape check the ids filter applies, so what can be saved and
# what can be asked for are one definition, not two that drift.
from job_filters import is_job_id

SAVED_PREFIX = "#saved#"


def saved_id(job_id: str) -> str:
    """The sort key for one saved job, validated on the way in.

    Validating here rather than at each call site is what makes the
    check unskippable: every write goes through this function, so a path
    segment that is not a job id cannot become a row.
    """
    if not is_job_id(job_id):
        raise ValueError("job_id must be 8-64 hex characters")
    return SAVED_PREFIX + job_id


def is_saved_id(alert_id) -> bool:
    return isinstance(alert_id, str) and alert_id.startswith(SAVED_PREFIX)


def job_id_of(item: dict) -> str | None:
    """The job a stored row points at.

    Prefers the attribute and falls back to the key. They agree for
    every row saved_id() wrote; the fallback is for the day a row is
    written by something else, so a star is never silently unreadable.
    """
    job_id = item.get("job_id")
    if isinstance(job_id, str) and job_id:
        return job_id
    alert_id = item.get("alert_id")
    return alert_id[len(SAVED_PREFIX):] if is_saved_id(alert_id) else None
