"""Clock conversions for sources that stamp times without a zone.

Only one so far. It lives here rather than in probe.py because the
loader's migration needs it too, for rows stored before the conversion
existed, and the loader cannot import probe.
"""

from datetime import datetime, timedelta, timezone


def israel_local_to_utc(s):
    """An ISO timestamp with no zone, read as Israel local time, in UTC.

    RedMatch's activationDate is the server's own clock and carries no
    offset: "2026-09-20T08:56:47.187". Stored as it came, SQLite's
    datetime() read it as UTC and the board's newest-first order put a
    Clalit listing three hours ahead of one really posted later, while
    the row's own age (the browser reads a bare timestamp as local time)
    said the opposite. That the clock is Israel's shows in the feed
    itself: 559 activations fall between 07:00 and 15:00, a working day
    here and a strange one anywhere else.

    Israel's clock change is a fixed rule since 2013: forward at 02:00 on
    the Friday before the last Sunday of March, back at 02:00 on the last
    Sunday of October. Written out rather than looked up because the
    Lambda image is not guaranteed a zoneinfo database. Anything that
    already carries an offset, or does not parse, is returned as it came.
    """
    if not s:
        return s
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return s
    if t.tzinfo is not None:
        return s
    last_sunday_march = datetime(t.year, 3, 31) - timedelta(days=(datetime(t.year, 3, 31).weekday() + 1) % 7)
    dst_start = last_sunday_march - timedelta(days=2) + timedelta(hours=2)
    last_sunday_october = datetime(t.year, 10, 31) - timedelta(days=(datetime(t.year, 10, 31).weekday() + 1) % 7)
    dst_end = last_sunday_october + timedelta(hours=2)
    offset = 3 if dst_start <= t < dst_end else 2
    return (t - timedelta(hours=offset)).replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
