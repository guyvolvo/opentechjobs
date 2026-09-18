"""Domains that are the same company as another domain on the board.

The loader already catches two domains resolving to one board: same ATS,
same token, one of them is an alias (see _demote_alias in
loader/load_to_sqlite.py). It cannot catch one company on two different
ATSes, because nothing about the two boards matches except the jobs.

Wayve was that. Discovery guessed the token "wayve" for two domains and
found a board on each: wayve.ai on Greenhouse and wayve.fr on Ashby.
Every role showed twice, 142 titles in common, and wayve.fr is not even
Wayve's site. It belongs to a French drinking-water company, which is
where the second logo came from. Reported live from a screenshot.

A scan of all 144,255 open jobs found no other pair like it. Title
overlap alone is not proof: weave.com and weave.ai share a few generic
titles and are two different companies. So this list is hand-verified,
like company_aliases.REAL_DOMAIN, and never filled in automatically.

Which one to keep: the board the company's own careers page links to.
wayve.ai/careers embeds the Greenhouse board.

The loader closes a listed domain's jobs and stops treating it as
resolved on every load, so it drops out of known.json and the fast poll.
probe.py skips it in a batch, so discovery cannot bring it back.
"""

# Duplicate domain -> the domain whose board is kept.
SAME_COMPANY = {
    "wayve.fr": "wayve.ai",
    # SentinelOne is on the board as sentinellabs.io, the domain its
    # Greenhouse board is named for. Pinning sentinelone.com to the same
    # board on 2026-09-18 just made the loader demote it as an alias
    # every day, so this says so once instead.
    "sentinelone.com": "sentinellabs.io",
}
