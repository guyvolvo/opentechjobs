"""Followed boards wait minutes, not hours. Orphaned rows go away.

Reported live on 2026-09-21: a ScaleOps posting took 72 minutes to reach
the reader who had an alert on it. Nothing was broken. ScaleOps posts
rarely, so it had backed off to the four-hour ceiling, and 8,055 of
10,104 boards were sitting there with it. Waiting is right for a board
nobody watches and wrong for one somebody asked about.

The same look at the state file turned up 611 rows for companies that
had left the list. due() walks the company list and looks each one up,
so those were never swept and never would be.

Run directly, no framework:  python tests/test_scrape_state_watched.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))

import scrape_state  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


def quiet_until_ceiling(domain, watched=frozenset(), polls=40):
    """Poll one board that never changes, and see where it settles."""
    state = {}
    for _ in range(polls):
        scrape_state.record(state, [{"domain": domain, "ats": "greenhouse", "unchanged": True}],
                            now=NOW, watched=watched)
    return state[domain]["interval_s"]


unfollowed = quiet_until_ceiling("scaleops.com")
followed = quiet_until_ceiling("scaleops.com", watched=frozenset({"scaleops.com"}))
check("a board nobody follows still backs off to four hours",
      unfollowed == scrape_state.CEILING_S == 14400, repr(unfollowed))
check("a board somebody has an alert on stops at thirty minutes",
      followed == scrape_state.WATCHED_CEILING_S == 1800, repr(followed))
check("which is eight times sooner than before", unfollowed / followed == 8)

# The reason this matters is the board that is ALREADY parked at four
# hours when the first alert on it is created. It has to come down on
# its own, without anyone resetting it by hand.
state = {"scaleops.com": {"interval_s": 14400, "next_at": NOW.isoformat()}}
scrape_state.record(state, [{"domain": "scaleops.com", "ats": "greenhouse", "unchanged": True}],
                    now=NOW, watched=frozenset({"scaleops.com"}))
check("a board already at the ceiling drops to it on the next quiet poll",
      state["scaleops.com"]["interval_s"] == 1800, repr(state["scaleops.com"]["interval_s"]))

# A posting still resets to the floor for everyone. Following a board
# must not make it slower in the one case that matters most.
state = {}
scrape_state.record(state, [{"domain": "wiz.io", "ats": "greenhouse", "content_hash": "abc"}],
                    now=NOW, watched=frozenset({"wiz.io"}))
check("a change still resets to the floor, followed or not",
      state["wiz.io"]["interval_s"] == scrape_state.FLOOR_S == 300)

# An empty watched set is what every failure upstream produces, so it
# has to mean "behave exactly as before" rather than anything clever.
check("no watched set at all schedules the way it always did",
      quiet_until_ceiling("x.com", watched=frozenset()) == 14400)

# Pruning. Two orphans among fifty rows, which is the shape of the real
# file: 611 among 10,104 when this was found. A fixture with two rows and
# one orphan would trip the guard below instead, and prove nothing.
state = {f"live{i}.com": {"interval_s": 300} for i in range(48)}
state["gone.com"] = {"interval_s": 1200}
state["also-gone.io"] = {"interval_s": 1200}
entries = [{"domain": f"live{i}.com"} for i in range(48)] + [{"domain": "new.com"}]
dropped = scrape_state.prune(state, entries)
check("rows with no company behind them are dropped",
      dropped == 2 and "gone.com" not in state and "also-gone.io" not in state,
      repr((dropped, len(state))))
check("everything still in the list is untouched", len(state) == 48)
check("a company with no row yet is left alone, not invented", "new.com" not in state)

# Domains are compared without case, because the two lists are written
# by different code paths and one of them could shout.
state = {"Wiz.IO": {"interval_s": 300}}
check("case does not decide whether a board is an orphan",
      scrape_state.prune(state, [{"domain": "wiz.io"}]) == 0 and "Wiz.IO" in state)

# The guard. A discovery run that produced a short list must not be able
# to wipe everyone's backoff and validators.
state = {f"c{i}.com": {"interval_s": 300} for i in range(100)}
kept = scrape_state.prune(state, [{"domain": "c0.com"}])
check("a company list that lost almost everything is refused, not obeyed",
      kept == 0 and len(state) == 100, repr((kept, len(state))))
state = {f"c{i}.com": {"interval_s": 300} for i in range(100)}
ok = scrape_state.prune(state, [{"domain": f"c{i}.com"} for i in range(95)])
check("a handful of departures is well within the guard and goes through",
      ok == 5 and len(state) == 95, repr((ok, len(state))))
check("an empty company list is refused outright",
      scrape_state.prune({"a.com": {}}, []) == 0)

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
