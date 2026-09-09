"""A salary estimator learned from the listings that disclose real pay.

The hand-built Israeli table in probe.py covers about 1% of this board,
because it is the only market we have a table for and Israeli employers
publish nothing. Meanwhile roughly one listing in ten states a real
range, because US pay transparency laws made that field mandatory and
Ashby surfaces it. That is a few thousand labelled rows we already scrape
and currently throw away after display.

This turns them into an estimator for the listings next to them. It is
deliberately not a model in the machine-learning sense: it is a table of
medians over cells, with a backoff chain and a minimum sample count, so
every number it produces can be traced to a specific set of real
listings. That matters more than squeezing out error here, because the
output is shown to job seekers as a figure about their own pay.

The cell order comes from measurement, not intuition (see salary_eval.py).
Against the disclosed set, company and seniority carry the signal and the
role taxonomy actively hurts, scoring worse than predicting one number
for everything. So the chain leads with company and seniority, and role
never appears in it.

Two gates decide whether anything is shown at all:

MIN_ROWS is how many real listings a cell needs before its median is
worth quoting. Below that the chain backs off to a broader cell.

MAX_REL_SPREAD is how wide the resulting range may be. A cell whose
middle half still spans more than this is a cell that has not actually
told us anything, and the honest output there is nothing. The Israeli
table learned this the hard way by shipping ₪20K-100K for months.
"""

import collections
import json
import re
import statistics
from pathlib import Path

MIN_ROWS = 5
MAX_REL_SPREAD = 1.5

# The band shown to a reader, as percentiles of the cell. Chosen from a
# sweep over p10-p90, p15-p85, p20-p80 and p25-p75 at three gate widths
# and two sample floors, scored by 5-fold cross validation on 2,179
# disclosed rows.
#
# The tension is entirely between how often the band contains the truth
# and how wide it has to be to manage it. A p25-p75 band hits 56% of the
# time, which is worse than a coin toss and not worth printing. p10-p90
# hits 73% but runs 0.90 wide. p15-p85 sits at 67% and 0.77, which is
# where the curve stops paying for itself.
#
# Worth saying plainly: these bands are wider than the Israeli table's
# 0.29, and that is not the learned model doing worse. The table's
# narrowness is an assertion nobody has ever checked. This width is the
# real dispersion of real salaries, measured.
BAND_LOW, BAND_HIGH = 15, 85

# Metro rather than raw location string. Ashby locations are free text
# ("San Carlos  - Hybrid", "SF Bay Area", "US - New York"), so grouping
# on them directly gives thousands of cells of one row each. Cost of
# living is the thing location is a proxy for, and it moves by metro.
METROS = [
    ("us-sf", r"san francisco|sf bay|bay area|palo alto|mountain view|san mateo|menlo park"
              r"|sunnyvale|santa clara|san jose|oakland|berkeley|san carlos|redwood city|cupertino"),
    ("us-nyc", r"new york|nyc|brooklyn|manhattan"),
    ("us-sea", r"seattle|bellevue|redmond|kirkland"),
    ("us-bos", r"boston|cambridge, ma|somerville|waltham"),
    ("us-la", r"los angeles|santa monica|pasadena|el segundo|ventura|san diego|irvine|culver city"),
    ("us-tex", r"austin|dallas|houston|san antonio"),
    ("us-dc", r"washington, ?d\.?c|arlington, va|bethesda|reston"),
    ("us-chi", r"chicago|evanston"),
    ("us-den", r"denver|boulder|salt lake"),
    ("us-atl", r"atlanta|miami|charlotte|raleigh|durham|nashville"),
    ("us-remote", r"remote|anywhere|distributed"),
    ("uk", r"london|manchester, uk|edinburgh|united kingdom"),
    ("ca", r"toronto|vancouver|montreal|ottawa|waterloo"),
    ("il", r"israel|tel aviv|herzliya|haifa|jerusalem|ra'?anana|petah|netanya|beer ?sheva"),
]
METRO_PATTERNS = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in METROS]


def metro_of(location: str | None) -> str | None:
    """A coarse market label, or None when the location says nothing we
    can place. None is common and is not a failure.
    """
    if not location:
        return None
    for name, pattern in METRO_PATTERNS:
        if pattern.search(location):
            return name
    return None


# Ashby joins extras onto the base range with a bullet: commission, a
# sign-on bonus, equity, "Multiple Ranges". Only the first clause is the
# salary, and annualising a $10K commission as an hourly rate produces a
# $10M job, which is exactly what the first version of this did.
#
# The lookbehind keeps R$ (Brazilian real) from reading as a dollar sign.
MONEY = re.compile(
    r"(?<![A-Za-z])(?P<cur>US\$|CA\$|A\$|SGD|PLN|\$|£|€|₪)\s?(?P<n>[\d,]+(?:\.\d+)?)(?P<k>K?)\b",
    re.IGNORECASE,
)


def parse_disclosed(text: str) -> tuple[str, float, float] | None:
    """(currency, low, high) annualised, or None for anything unusable.

    Mixed-currency strings are dropped rather than guessed at, and so is
    anything outside a sane annual band, which catches both parse errors
    and the per-hour rows that are really contract work.
    """
    base = re.split(r"[•·]", text)[0]
    lowered = base.lower()
    per_year = 2080 if "per hour" in lowered else 12 if "per month" in lowered else 1
    found = list(MONEY.finditer(base))
    if not found:
        return None
    currency = found[0].group("cur").upper()
    if any(m.group("cur").upper() != currency for m in found):
        return None
    values = [
        float(m.group("n").replace(",", "")) * (1000 if m.group("k") else 1) * per_year
        for m in found
    ]
    values = [v for v in values if 5_000 < v < 2_000_000]
    if not values:
        return None
    return currency, min(values), max(values)


# Broadest last. Each entry is (name, key function); a None key means the
# row cannot be placed at this level and the chain moves on.
#
# Role is deliberately absent. On the disclosed set a role cell scores
# 18.6% median error against a 17.9% flat baseline over the same rows,
# so including it would make the estimate worse while making it look
# more precise, which is the worst combination available.
def _company(job):
    return job.get("company_domain")


def _seniority(job):
    return job.get("seniority")


CHAIN = [
    ("company+metro+seniority", lambda j: _all(_company(j), metro_of(j.get("location")), _seniority(j))),
    ("company+seniority", lambda j: _all(_company(j), _seniority(j))),
    ("metro+dept+seniority", lambda j: _all(metro_of(j.get("location")), j.get("department"), _seniority(j))),
    ("metro+seniority", lambda j: _all(metro_of(j.get("location")), _seniority(j))),
    ("dept+seniority", lambda j: _all(j.get("department"), _seniority(j))),
    ("company+metro", lambda j: _all(_company(j), metro_of(j.get("location")))),
    ("seniority", lambda j: _all(_seniority(j))),
]


def _all(*parts):
    """The tuple, or None if any part is missing. A cell keyed on a
    missing value would silently pool every row that lacks it.
    """
    return parts if all(p is not None and p != "" for p in parts) else None


class SalaryModel:
    """Cells of disclosed pay, one set per currency, with a backoff chain."""

    def __init__(self, cells: dict, currency: str):
        self.cells = cells
        self.currency = currency

    @classmethod
    def build(cls, rows: list[tuple[dict, float]], currency: str) -> "SalaryModel":
        """rows is [(job, annual pay)] already filtered to one currency."""
        cells: dict = {}
        for level, key_of in CHAIN:
            grouped = collections.defaultdict(list)
            for job, pay in rows:
                key = key_of(job)
                if key is not None:
                    grouped[key].append(pay)
            cells[level] = {k: sorted(v) for k, v in grouped.items() if len(v) >= MIN_ROWS}
        cells["global"] = sorted(pay for _, pay in rows)
        return cls(cells, currency)

    def predict(self, job: dict) -> dict | None:
        """The band of the tightest cell this job lands in.

        Returns {low, high, level, n} or None. Percentiles rather than the
        cell's full range: the tails of a cell are other people's jobs,
        and a band wide enough to always be right is not worth printing.
        """
        for level, key_of in CHAIN:
            key = key_of(job)
            if key is None:
                continue
            values = self.cells.get(level, {}).get(key)
            if not values:
                continue
            low, high = _band(values)
            if low <= 0 or (high - low) / low > MAX_REL_SPREAD:
                continue
            return {"low": low, "high": high, "level": level, "n": len(values)}
        return None

    def to_json(self) -> str:
        return json.dumps({"currency": self.currency, "cells": _keys_to_str(self.cells)})

    @classmethod
    def from_json(cls, text: str) -> "SalaryModel":
        raw = json.loads(text)
        return cls(_keys_from_str(raw["cells"]), raw["currency"])

    def save(self, path: Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SalaryModel":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def _band(sorted_values: list[float]) -> tuple[float, float]:
    """BAND_LOW and BAND_HIGH as percentiles. A cell of identical values
    collapses to a zero-width band, which is fine and true: five listings
    all paying the same is a real answer.
    """
    percentiles = statistics.quantiles(sorted_values, n=100)
    return percentiles[BAND_LOW - 1], percentiles[BAND_HIGH - 1]


def _keys_to_str(cells: dict) -> dict:
    """Tuple keys can't survive JSON. Joined on a unit separator, which
    cannot appear in a domain, a metro label or a department name.
    """
    out = {}
    for level, group in cells.items():
        if level == "global":
            out[level] = group
        else:
            out[level] = {"\x1f".join(str(p) for p in k): v for k, v in group.items()}
    return out


def _keys_from_str(cells: dict) -> dict:
    out = {}
    for level, group in cells.items():
        if level == "global":
            out[level] = group
        else:
            out[level] = {tuple(k.split("\x1f")): v for k, v in group.items()}
    return out
