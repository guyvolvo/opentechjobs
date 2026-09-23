"""The monthly shekel range a salary string states, as two integers.

`salary_text` is prose. It has to be, because it carries whatever the
employer wrote, and a board that rewrites an employer's own figure into
a tidier one is inventing evidence. But prose cannot be filtered on, so
the loader derives a pair of numbers beside it and the board's salary
filter reads those.

Shekels only, and that is the whole design rather than a first pass.
Measured on the live snapshot (2026-09-23, 968,659 open listings):

    disclosed  20,264   USD 18,390 · EUR 1,075 · GBP 565 · other 234
    estimated  72,972   the model's own cells, in the currency it
                        learned from, so overwhelmingly USD
    table       2,795   probe.py's Israeli table, every one of them
                        shekels, every one of them monthly gross
    (none)    872,628

Not one disclosed listing is in shekels. Every shekel string on the
board was written by probe.py from its own Israeli table, which is why
the period can be taken as monthly without asking the text: we wrote it.

Converting the other 90,000 would need an exchange rate, and this
project has no source of one. A rate pinned in the code would be a
number nobody measured, drifting from the truth a little more each day,
and it would land inside a filter where the reader cannot see it to
doubt it. So those rows get no numbers and the filter leaves them
alone, which is this board's usual answer to a fact it does not have.

Per-hour strings are skipped for the same reason: turning one into a
month means choosing how many hours a month is, and that choice would
be ours rather than the listing's.
"""

import re

# Two passes, because a range signs its currency once and then stops.
# Every shekel string on the board is written "₪35K–45K": the sign is on
# the low end and the high end is a bare number. Matching only
# sign-prefixed figures read that as a single value and every range came
# out with its floor for a ceiling, so the filter would have put a
# ₪35K–45K job outside a ₪40K–50K search.
#
# So _MARK establishes the currency and _NUMBER reads the figures. Kept
# separate from salary_model.MONEY rather than imported because the two
# ask different questions: that one annualises anything it can read,
# this one wants a monthly figure in shekels or nothing.
_MARK = re.compile(r"₪")
_NUMBER = re.compile(r"(?P<n>\d[\d,]*(?:\.\d+)?)\s?(?P<k>K)?\b", re.IGNORECASE)

# Ashby joins extras onto the base range with a bullet (commission, a
# sign-on bonus, equity). Only the first clause is the salary. Same
# split salary_model.parse_disclosed makes, and for the same reason: a
# ₪10K sign-on read as the top of the range moves the listing into a
# bracket it does not belong in.
_EXTRAS = re.compile(r"[•·]")

# A month's gross in shekels, outside which the parse is wrong rather
# than the job unusual. The floor sits under minimum wage (about ₪5,880
# a month in 2026) so a real junior posting is never dropped; the
# ceiling is high enough for any executive figure the board carries.
_FLOOR = 2_000
_CEILING = 400_000


def monthly_ils(text: str | None) -> tuple[int, int] | None:
    """(low, high) monthly gross shekels, or None for anything else.

    None covers every honest miss: a string in another currency, an
    hourly rate, a figure that parsed to something impossible, and an
    empty column. The caller stores NULL for all of them alike, because
    "we could not read this" and "there is no figure here" are the same
    fact as far as a filter is concerned.
    """
    if not text:
        return None
    base = _EXTRAS.split(text)[0]
    lowered = base.lower()
    if "per hour" in lowered or "hourly" in lowered or "/hr" in lowered:
        return None
    if not _MARK.search(base):
        return None
    # A string naming a second currency alongside shekels is ambiguous
    # about which one the range is in, so it is not read at all.
    if re.search(r"(?<![A-Za-z])(US\$|CA\$|A\$|SGD|PLN|\$|£|€)", base):
        return None
    values = [
        float(m.group("n").replace(",", "")) * (1000 if m.group("k") else 1)
        for m in _NUMBER.finditer(base)
    ]
    # An annual figure divided into months. Nothing on the board says
    # this today (every shekel row is ours and monthly), and it is here
    # so that the first Israeli employer to publish an annual range is
    # read correctly rather than filed as a ₪400K/month job.
    if "per year" in lowered or "annual" in lowered or "per annum" in lowered:
        values = [v / 12 for v in values]
    values = [round(v) for v in values if _FLOOR <= v <= _CEILING]
    if not values:
        return None
    return min(values), max(values)
