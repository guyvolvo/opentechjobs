"""Shared shard-assignment math for the fast re-poll cycle (scrape_handler.py)
and the partition merge step (loader/merge_partitions.py).

Both need the exact same deterministic domain -> shard mapping. If they
drifted (different SHARD_SIZE, different sort key), the merge step's own
tombstoning -- dropping a company's row from a partition it no longer
belongs to, see merge_partitions.py's docstring -- would disagree with
which partition scrape_handler.py is actually writing that company into,
silently treating live, current data as an orphan and losing it on every
merge. One source of truth instead of two copies that can go out of sync,
the same lesson as the 60s loader-timeout bug found in three separate
handlers this same day.

Not used for scrape_workday_handler.py's own partition: pinned companies
(companies.yml) don't go through this scheme -- see that module's own
docstring. Its partition is a fixed name ("workday"), always merged in
full, never subject to reassignment.
"""

import time

# ~50 companies/shard measured comfortably under a minute per invocation
# with real network I/O to each board's own API. Must match
# scrape_handler.py's own SHARD_SIZE -- they're the same constant, kept
# here so there's only one place to change it.
SHARD_SIZE = 50


def num_shards_for(n_companies: int) -> int:
    return max(1, -(-n_companies // SHARD_SIZE))  # ceil division


def ordered_domains(known: list[dict]) -> list[str]:
    """Sorted by domain, not by dict/JSON ordering -- so which companies
    land in which shard doesn't shuffle between invocations just because
    known.json's own row order changed. Only NUM_SHARDS growing (as
    known.json grows) should ever move a company to a different shard.
    """
    return [e.get("domain", "") for e in sorted(known, key=lambda e: e.get("domain", ""))]


def build_shard_map(known: list[dict]) -> dict[str, int]:
    """domain -> its current shard index. Recomputed fresh from the full
    known-company list every call, never cached across calls -- NUM_SHARDS
    growing as that list grows is exactly what moves a company to a
    different shard, so there's no stable identity to reuse.
    """
    domains = ordered_domains(known)
    return {domain: i // SHARD_SIZE for i, domain in enumerate(domains)}


def current_shard_index(num_shards: int, schedule_interval_s: int) -> int:
    """Which shard runs THIS invocation, from wall-clock time -- not
    carried in the event payload, so scaling NUM_SHARDS up as the company
    list grows needs no scheduler config change. schedule_interval_s must
    match the caller's own EventBridge schedule (scrape_handler.py's
    SCHEDULE_INTERVAL_S) or shard selection and the actual invocation
    cadence drift apart.
    """
    return int(time.time() // schedule_interval_s) % num_shards
