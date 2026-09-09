"""Measure the salary estimator against the listings that disclose real pay.

About one listing in ten on this board states a real range, all of them
via Ashby, because Ashby surfaces the compensation field that US pay
transparency laws made mandatory. That is a few thousand labelled rows
sitting in our own database, and it is the only honest way to argue about
whether a change to the estimator helps.

Two things it will not do, both worth knowing before reading any number
it prints:

Not one of those disclosed ranges is Israeli. Israel has no pay
transparency law and Israeli employers do not publish ranges, so the
Shekel estimator in probe.py cannot be scored against ground truth at
all. What this measures is the machinery around it: whether a role
taxonomy predicts pay, what an unstated level is worth, how wide the
ranges we ship actually are. Those transfer. Absolute Israeli accuracy
does not, and nothing here should be read as claiming otherwise.

And the disclosed population is not the board's population. It skews US,
startup, and heavily towards sales and operations rather than
engineering. Treat it as evidence about mechanisms, not as a census.

    python salary_eval.py                  # sample the live API
    python salary_eval.py --pages 60       # more rows, slower
    python salary_eval.py --cache rows.json
"""

import argparse
import collections
import json
import re
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from probe import _ROLE_CATEGORY_RULES, _estimate_salary, IL_KEYWORDS  # noqa: E402
from salary_model import SalaryModel, parse_disclosed  # noqa: E402

API = "https://opentechjobs.org/api/jobs"


def fetch(pages: int, per_page: int = 500) -> list[dict]:
    """Even offsets across the whole corpus, not the first N pages, so
    the sample isn't just the newest listings.
    """
    def get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "opentechjobs-salary-eval/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    total = get(f"{API}?limit=1&confidence=all")["total"]
    jobs = []
    for i in range(pages):
        offset = int(i * total / pages)
        try:
            jobs += get(f"{API}?limit={per_page}&offset={offset}&confidence=all")["jobs"]
        except Exception as e:
            print(f"  page at offset {offset} failed, skipped: {e!r}", file=sys.stderr)
    return jobs


def role_category(title: str) -> str | None:
    for category, pattern in _ROLE_CATEGORY_RULES:
        if pattern.search(title):
            return category
    return None


def score(rows, key_of, min_rows=5):
    """Leave-one-out median absolute percentage error for "predict this
    group's median", against predicting one number for every row.

    Leave-one-out because a group of one would otherwise score itself
    perfectly, which is how a first pass here credited the role taxonomy
    with explaining 99% of pay.

    The baseline is recomputed on exactly the rows the signal covers.
    Signals cover different subsets, and comparing a narrow signal
    against a baseline drawn from everything flatters it.
    """
    groups = collections.defaultdict(list)
    for job, pay in rows:
        key = key_of(job)
        if key is not None:
            groups[key].append(pay)
    covered = [(j, p) for j, p in rows
               if key_of(j) is not None and len(groups[key_of(j)]) >= min_rows]
    if not covered:
        return None
    flat = statistics.median([p for _, p in covered])
    baseline = statistics.median([abs(flat - p) / p for _, p in covered])
    errors = []
    for job, pay in covered:
        others = [v for v in groups[key_of(job)] if v != pay] or groups[key_of(job)]
        errors.append(abs(statistics.median(others) - pay) / pay)
    return statistics.median(errors), baseline, len(covered)


def backtest(rows, folds: int = 5, seed: int = 7):
    """Score the hierarchical estimator by k-fold cross validation.

    Folds rather than leave-one-out, because the cells are sample-gated.
    Pulling one row out can drop a cell below MIN_ROWS and change which
    level of the chain answers, so the model genuinely has to be rebuilt
    without the held-out rows rather than adjusted in place. Five rebuilds
    are affordable; adjusting a cell would be neither affordable nor true.

    Three numbers matter and they trade against each other. Error is how
    far the middle of the band sits from the real figure. Hit rate is how
    often the real figure lands inside the band at all, which is what a
    reader actually experiences. Width is what the band costs them: one
    wide enough to always be right tells nobody anything.
    """
    import random

    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    buckets = [shuffled[i::folds] for i in range(folds)]

    errors, widths, flat_errors = [], [], []
    hits = predicted = 0
    by_level = collections.Counter()
    for i in range(folds):
        train = [r for k, b in enumerate(buckets) if k != i for r in b]
        model = SalaryModel.build(train, "$")
        flat = statistics.median([p for _, p in train])
        for job, pay in buckets[i]:
            flat_errors.append(abs(flat - pay) / pay)
            got = model.predict(job)
            if not got:
                continue
            predicted += 1
            middle = (got["low"] + got["high"]) / 2
            errors.append(abs(middle - pay) / pay)
            widths.append((got["high"] - got["low"]) / got["low"])
            hits += got["low"] <= pay <= got["high"]
            by_level[got["level"]] += 1

    total = sum(len(b) for b in buckets)
    print("\nHIERARCHICAL ESTIMATOR, 5-FOLD CROSS VALIDATION")
    print(f"  coverage      {predicted:,}/{total:,} rows = {predicted / total:.0%}")
    if not errors:
        print("  nothing predicted, every cell too thin at this sample size")
        return
    print(f"  median error  {statistics.median(errors):.1%}   "
          f"(flat baseline on the same corpus: {statistics.median(flat_errors):.1%})")
    print(f"  hit rate      {hits / predicted:.0%} of real figures land inside the band")
    print(f"  median width  {statistics.median(widths):.2f} of the band's own floor")
    print("  which cell answered:")
    for level, n in by_level.most_common():
        print(f"    {level:<24}{n:>6}{n / predicted:>6.0%}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=40, help="sample pages of 500 across the corpus")
    ap.add_argument("--cache", type=Path, help="read/write the sample here instead of refetching")
    args = ap.parse_args()

    if args.cache and args.cache.exists():
        jobs = json.loads(args.cache.read_text(encoding="utf-8"))
        print(f"{len(jobs):,} listings from {args.cache}")
    else:
        jobs = fetch(args.pages)
        if args.cache:
            args.cache.write_text(json.dumps(jobs), encoding="utf-8")
        print(f"{len(jobs):,} listings sampled from the live API")

    # One row per company and exact figure. A company posting the same
    # range on eight near-identical reqs would otherwise let a
    # leave-one-out fold predict a row from its own duplicate, which
    # roughly doubled the apparent value of the company signal.
    rows, seen = [], set()
    for job in jobs:
        if not job.get("salary_text") or job.get("salary_is_estimate"):
            continue
        parsed = parse_disclosed(job["salary_text"])
        if not parsed or parsed[0] != "$":
            continue
        pay = (parsed[1] + parsed[2]) / 2
        key = (job["company_domain"], round(pay))
        if key in seen:
            continue
        seen.add(key)
        rows.append((job, pay))

    disclosed = sum(1 for j in jobs if j.get("salary_text") and not j.get("salary_is_estimate"))
    print(f"{disclosed:,} disclose a real range ({disclosed / len(jobs):.1%}), "
          f"{len(rows):,} usable USD rows after de-duplication\n")

    print("WHAT PREDICTS PAY")
    print("Median absolute error against the same rows' own flat baseline.")
    print(f"{'signal':<26}{'error':>8}{'baseline':>10}{'gain':>8}{'rows':>7}")
    signals = [
        ("company", lambda j: j["company_domain"]),
        ("seniority", lambda j: j.get("seniority")),
        ("department", lambda j: j.get("department")),
        ("role category (probe.py)", lambda j: role_category(j["title"])),
        ("company x seniority", lambda j: (j["company_domain"], j["seniority"]) if j.get("seniority") else None),
        ("role x seniority", lambda j: (role_category(j["title"]), j["seniority"])
            if role_category(j["title"]) and j.get("seniority") else None),
    ]
    for name, key_of in signals:
        result = score(rows, key_of)
        if not result:
            print(f"{name:<26}{'too few rows':>8}")
            continue
        error, baseline, n = result
        print(f"{name:<26}{error:>7.1%}{baseline:>10.1%}{baseline - error:>+8.1%}{n:>7}")

    print("\nWHAT AN UNSTATED LEVEL IS WORTH")
    by_level = collections.defaultdict(list)
    for job, pay in rows:
        by_level[job.get("seniority")].append(pay)
    unstated = statistics.median(by_level[None]) if by_level.get(None) else None
    for level in ["intern", "junior", "mid", None, "senior", "staff", "principal",
                  "lead", "manager", "director", "exec"]:
        values = by_level.get(level) or []
        if len(values) < 5:
            continue
        median = statistics.median(values)
        ratio = f"{median / unstated:.2f}x unstated" if unstated else ""
        print(f"  {str(level or 'UNSTATED'):<10}{len(values):>6} rows   {median:>10,.0f}   {ratio}")

    backtest(rows)

    print("\nWHAT WE ACTUALLY SHIP, ON ISRAELI LISTINGS")
    israeli = [j for j in jobs
               if j.get("location") and any(k in j["location"].lower() for k in IL_KEYWORDS)]
    widths, quiet = [], 0
    for job in israeli:
        estimate = _estimate_salary(job["title"], None, job.get("seniority"))
        if estimate:
            low, high = estimate
            widths.append((high - low) / low)
        else:
            quiet += 1
    if widths:
        widths.sort()
        print(f"  {len(israeli):,} Israeli listings, {len(widths):,} estimated, {quiet:,} left blank")
        print(f"  range width as a fraction of its own floor: "
              f"median {statistics.median(widths):.2f}, p90 {widths[int(0.9 * len(widths))]:.2f}")
        print(f"  wider than their own floor: {sum(1 for w in widths if w > 1.0) / len(widths):.0%}")
    else:
        print("  no Israeli listings in this sample")
    return 0


if __name__ == "__main__":
    sys.exit(main())
