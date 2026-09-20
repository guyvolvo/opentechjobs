"""Coordinates for the cities the listings name, from OpenStreetMap.

Reads the distinct (country, city) pairs among open listings in a
snapshot, asks Nominatim for each one it has not seen, and writes
frontend/geo/cities.json: {"CC|City": [lat, lon]} plus one "CC" entry
per country for the country's own point. The file is committed, so a
run only pays for pairs that are new since the last one.

Nominatim's policy is one request a second with a contact in the
user agent, and this obeys it. A city with fewer than --min listings is
skipped: 11,739 pairs exist and the long tail is one listing each, but
1,855 pairs at ten or more cover most of the board.

    python loader/geocode_cities.py --db loader/jobs.db --min 10
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
from countries import label_for  # noqa: E402

OUT = ROOT / "frontend" / "geo" / "cities.json"
UA = "OpenTechJobs geocoder (https://opentechjobs.org; guyvoloshin@gmail.com)"
API = "https://nominatim.openstreetmap.org/search"


def lookup(sess, q, cc=None):
    params = {"q": q, "format": "jsonv2", "limit": 1}
    if cc:
        params["countrycodes"] = cc.lower()
    try:
        r = sess.get(API, params=params, headers={"User-Agent": UA}, timeout=30)
        if r.status_code != 200:
            return None
        hits = r.json()
    except (requests.RequestException, ValueError):
        return None
    if not hits:
        return None
    return [round(float(hits[0]["lat"]), 3), round(float(hits[0]["lon"]), 3)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "loader" / "jobs.db")
    ap.add_argument("--min", type=int, default=10)
    args = ap.parse_args()

    geo = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT country, city, COUNT(*) n FROM jobs WHERE closed_at IS NULL AND city != '' AND country != ''"
        " AND country NOT LIKE '%,%' AND city NOT LIKE '%,%' GROUP BY country, city HAVING n >= ? ORDER BY n DESC",
        (args.min,)).fetchall()
    countries = sorted({r[0] for r in rows})
    todo = [("|".join((c, city)), f"{city}, {label_for(c)}", c) for c, city, _ in rows if "|".join((c, city)) not in geo]
    todo += [(c, label_for(c), None) for c in countries if c not in geo]
    print(f"{len(rows)} pairs at >= {args.min}, {len(geo)} already known, {len(todo)} to look up (~{len(todo) * 1.1 / 60:.0f} min)")

    sess = requests.Session()
    misses = 0
    for i, (key, q, cc) in enumerate(todo, 1):
        point = lookup(sess, q, cc)
        if point is None:
            misses += 1
            geo[key] = None  # remembered, so a miss is not asked again every run
        else:
            geo[key] = point
        if i % 25 == 0 or i == len(todo):
            OUT.write_text(json.dumps(geo, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
            print(f"  {i}/{len(todo)} ({misses} misses)", flush=True)
        time.sleep(1.1)
    OUT.write_text(json.dumps(geo, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    print(f"wrote {OUT} with {sum(1 for v in geo.values() if v)} points")


if __name__ == "__main__":
    main()
