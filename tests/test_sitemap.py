"""The generated sitemap lists what the board shows, and nothing else.

Run directly, no framework:  python tests/test_sitemap.py
"""

import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loader"))
sys.path.insert(0, str(ROOT / "api"))

import sitemap  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
tmp = Path(tempfile.mkdtemp())
db = tmp / "jobs.db"
conn = sqlite3.connect(db)
conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
conn.execute("INSERT INTO companies (domain, ats, first_seen, last_checked, company_name) VALUES ('wix.com','greenhouse','2026-09-01','2026-09-18','Wix')")


def job(jid, posted, first_seen="2026-09-10T08:00:00+00:00", closed=None, title="Engineer", desc="Build & ship"):
    conn.execute("INSERT INTO jobs (id, company_domain, ats, title, location, department, url, posted_at, first_seen, last_seen, closed_at, description, confidence)"
                 " VALUES (?, 'wix.com', 'greenhouse', ?, 'Tel Aviv, Israel', 'R&D', 'https://x/y', ?, ?, '2026-09-18T11:00:00+00:00', ?, ?, 'verified')",
                 (jid, title, posted, first_seen, closed, desc))


# Shard size is shrunk so sharding is exercised without 80,001 rows.
sitemap.SHARD = 3
job("a1", "2026-09-17T10:00:00+00:00")
job("a2", None)                                            # no source date: first_seen stands in
job("a3", "2026-09-16T10:00:00+00:00", title="Dev <Lead>")
job("a4", "2026-09-15T10:00:00+00:00")                    # second shard
job("closed1", "2026-09-14T10:00:00+00:00", closed="2026-09-17T00:00:00+00:00")
job("stale1", "2024-01-01T10:00:00+00:00")                # older than the freshness window
conn.commit()
conn.close()

docs = sitemap.build(db, NOW)
check("an index, a pages sitemap, the feed, two job shards and a company shard",
      sorted(docs) == ["feed.xml", "sitemap-companies-1.xml", "sitemap-jobs-1.xml", "sitemap-jobs-2.xml", "sitemap-pages.xml", "sitemap.xml"], repr(sorted(docs)))
companies = docs["sitemap-companies-1.xml"][0].decode("utf-8")
check("the company page is listed once, with the newest open listing's arrival as lastmod",
      companies.count("<loc>https://opentechjobs.org/company/wix.com</loc>") == 1
      and "<loc>https://opentechjobs.org/company/wix.com</loc><lastmod>2026-09-10T08:00:00Z</lastmod>" in companies, companies[:400])

idx = ET.fromstring(docs["sitemap.xml"][0])
ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
locs = [e.text for e in idx.findall("s:sitemap/s:loc", ns)]
check("the index names every file by absolute URL",
      locs == ["https://opentechjobs.org/sitemap-pages.xml", "https://opentechjobs.org/sitemap-jobs-1.xml",
               "https://opentechjobs.org/sitemap-jobs-2.xml", "https://opentechjobs.org/sitemap-companies-1.xml"], repr(locs))
check("index entries carry the build time", all(e.text == "2026-09-18T12:00:00Z" for e in idx.findall("s:sitemap/s:lastmod", ns)))

s1 = ET.fromstring(docs["sitemap-jobs-1.xml"][0])
s2 = ET.fromstring(docs["sitemap-jobs-2.xml"][0])
urls1 = [e.text for e in s1.findall("s:url/s:loc", ns)]
urls2 = [e.text for e in s2.findall("s:url/s:loc", ns)]
check("open, fresh listings only, at their own pages, in id order across shards",
      urls1 == [f"https://opentechjobs.org/job/{i}" for i in ("a1", "a2", "a3")]
      and urls2 == ["https://opentechjobs.org/job/a4"], repr((urls1, urls2)))
check("closed and outdated listings are left out",
      not any("closed1" in u or "stale1" in u for u in urls1 + urls2))
mods = {e.find("s:loc", ns).text.rsplit("/", 1)[1]: (e.find("s:lastmod", ns).text if e.find("s:lastmod", ns) is not None else None)
        for e in s1.findall("s:url", ns)}
check("lastmod is the posting date, in UTC with a Z", mods["a1"] == "2026-09-17T10:00:00Z", repr(mods))
check("a listing without a source date uses when the board first saw it", mods["a2"] == "2026-09-10T08:00:00Z", repr(mods))

pages = ET.fromstring(docs["sitemap-pages.xml"][0])
entries = {e.find("s:loc", ns).text: (e.find("s:lastmod", ns).text if e.find("s:lastmod", ns) is not None else None)
           for e in pages.findall("s:url", ns)}
check("the seven pages, with the board, the map and Explore dated to the snapshot and the rest undated",
      entries == {"https://opentechjobs.org/": None, "https://opentechjobs.org/board": "2026-09-18T12:00:00Z",
                  "https://opentechjobs.org/map": "2026-09-18T12:00:00Z",
                  "https://opentechjobs.org/stats": "2026-09-18T12:00:00Z", "https://opentechjobs.org/api/help": None,
                  "https://opentechjobs.org/contact": None, "https://opentechjobs.org/privacy": None}, repr(entries))

rss = ET.fromstring(docs["feed.xml"][0])
items = rss.findall("channel/item")
check("the feed is newest first and skips closed and stale listings",
      [i.find("link").text.rsplit("/", 1)[1] for i in items] == ["a1", "a3", "a4", "a2"],
      repr([i.find("link").text for i in items]))
first = items[0]
check("an item has a title with the company, a permalink guid, a date and a description",
      first.find("title").text == "Engineer at Wix" and first.find("guid").text == "https://opentechjobs.org/job/a1"
      and first.find("pubDate").text.startswith("Thu, 17 Sep 2026") and "Wix · Tel Aviv, Israel · R&D" in first.find("description").text,
      repr((first.find("title").text, first.find("pubDate").text, first.find("description").text)))
check("escaping survives a title with angle brackets and an ampersand in the blurb",
      any(i.find("title").text == "Dev <Lead> at Wix" for i in items) and "Build & ship" in items[0].find("description").text)
check("every document declares UTF-8 and parses as XML", all(b.startswith(b'<?xml version="1.0" encoding="UTF-8"?>') for b, _ in docs.values()))
check("content types are XML and RSS", docs["sitemap.xml"][1].startswith("application/xml") and docs["feed.xml"][1].startswith("application/rss+xml"))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
