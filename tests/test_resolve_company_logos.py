"""Rechecking stored logos, and taking a rejected one off the board.

Wix's listings showed GoDaddy's logo. Its recorded domain is parked, Google
returned the registrar's icon, and that answer was stored as found. Two
things kept it there: the resolver never looks at a company that already
has a logo, and the loaders never write a null over one. These tests are
about both halves of undoing that without ever taking down a good logo.

Run directly, no framework:  python tests/test_resolve_company_logos.py
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "loader"))

import resolve_company_logos as rcl  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


V = rcl.LOGO_CHECK_VERSION
GOOGLE = "https://www.google.com/s2/favicons?domain={}&sz=128"

# Which stored logos get looked at again.
check("an old Google-tier logo is rechecked",
      rcl.needs_recheck("acme.com", {"url": GOOGLE.format("acme.com"), "source": "google"}))
check("but not once it has passed the current checks",
      not rcl.needs_recheck("acme.com", {"url": GOOGLE.format("acme.com"), "source": "google", "check": V}))
check("an ATS logo is left alone: it comes from the company's own account",
      not rcl.needs_recheck("acme.com", {"url": "https://cdn/x.png", "source": "ats"}))
# This said "a site logo is left alone" until 2026-09-21, on the
# reasoning that the new checks only changed what Google returns. That
# was wrong. A site favicon runs through the same placeholder gate, so
# every one stored before a fingerprint was added kept it forever, and
# 264 companies were found wearing an image that belonged to somebody
# else. Two clusters of them were the WordPress default, which the
# blocklist already thought it had dealt with.
check("a site logo from before the current checks is looked at again",
      rcl.needs_recheck("acme.com", {"url": "https://acme.com/icon.png", "source": "site"}))
check("but not once it has passed them",
      not rcl.needs_recheck("acme.com", {"url": "https://acme.com/icon.png", "source": "site", "check": V}))
check("a miss is not a recheck (the miss retry handles those)",
      not rcl.needs_recheck("acme.com", {"url": None, "source": "none"}))
# wix2.com is aliased to wix.com. A logo stored before the alias existed
# was looked up at wix2.com, whatever tier found it.
check("a logo stored before its alias existed is rechecked",
      rcl.needs_recheck("wix2.com", {"url": "https://wix2.com/i.png", "source": "site"}))
check("and not again once it was looked up at the aliased domain",
      not rcl.needs_recheck("wix2.com", {"url": "https://www.wix.com/favicon.ico", "source": "site",
                                         "looked_at": "wix.com", "check": V}))

# What a fresh answer does to the stored one.
logos = {"wix2.com": {"url": GOOGLE.format("wix2.com"), "source": "google"}}
rcl.merge_entry(logos, "wix2.com", {"url": None, "source": "none", "looked_at": "wix2.com", "check": V})
check("a rejected Google logo is marked for removal",
      logos["wix2.com"].get("cleared") == GOOGLE.format("wix2.com"), repr(logos["wix2.com"]))

logos = {"acme.com": {"url": "https://cdn/acme.png", "source": "ats"}}
rcl.merge_entry(logos, "acme.com", {"url": None, "source": "none", "looked_at": "acme.com", "check": V})
check("a good ATS logo that fails one later lookup is not marked for removal",
      "cleared" not in logos["acme.com"], repr(logos["acme.com"]))

logos = {"wix2.com": {"url": "https://wix2.com/i.png", "source": "site"}}
rcl.merge_entry(logos, "wix2.com", {"url": None, "source": "none", "looked_at": "wix.com", "check": V})
check("a logo from before an alias is marked for removal when the alias finds nothing",
      logos["wix2.com"].get("cleared") == "https://wix2.com/i.png", repr(logos["wix2.com"]))

logos = {"wix2.com": {"url": GOOGLE.format("wix2.com"), "source": "google"}}
rcl.merge_entry(logos, "wix2.com", {"url": "https://www.wix.com/favicon.ico", "source": "site",
                                    "looked_at": "wix.com", "check": V})
check("a replacement logo just replaces, nothing to clear",
      "cleared" not in logos["wix2.com"] and logos["wix2.com"]["url"].endswith("favicon.ico"))

logos = {}
rcl.merge_entry(logos, "new.com", {"url": None, "source": "none", "looked_at": "new.com", "check": V})
check("a first-ever miss clears nothing", "cleared" not in logos["new.com"])


# Both loaders: a cleared URL comes off the board only while the board
# still holds that exact URL.
def board():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE companies (domain TEXT PRIMARY KEY, logo_url TEXT)")
    conn.executemany("INSERT INTO companies VALUES (?, ?)", [
        ("wix2.com", GOOGLE.format("wix2.com")),
        ("moved.com", "https://moved.com/newer-good-logo.png"),
        ("acme.com", None),
    ])
    return conn


LOGO_FILE = {
    "wix2.com": {"url": None, "source": "none", "cleared": GOOGLE.format("wix2.com")},
    # The board already moved on to a newer logo; the old cleared URL no
    # longer matches and must not take the new one down.
    "moved.com": {"url": None, "source": "none", "cleared": "https://moved.com/old.png"},
    "acme.com": {"url": "https://cdn/acme.png", "source": "ats"},
}


def logo_of(conn, domain):
    return conn.execute("SELECT logo_url FROM companies WHERE domain = ?", (domain,)).fetchone()[0]


loaders = []
try:
    import load_to_sqlite
    loaders.append(("load_to_sqlite", lambda conn: load_to_sqlite.apply_company_logos(
        conn, _write_logo_file())))
except Exception as e:  # pragma: no cover - reported, not hidden
    check("load_to_sqlite imports", False, repr(e))
try:
    import merge_partitions
    loaders.append(("merge_partitions", lambda conn: merge_partitions.apply_company_logos(conn, LOGO_FILE)))
except Exception as e:  # pragma: no cover
    check("merge_partitions imports", False, repr(e))


def _write_logo_file():
    f = Path(tempfile.mkdtemp()) / "company-logos.json"
    f.write_text(json.dumps(LOGO_FILE), encoding="utf-8")
    return f


for name, apply in loaders:
    conn = board()
    apply(conn)
    check(f"{name}: the rejected parked-domain logo is taken down", logo_of(conn, "wix2.com") is None,
          repr(logo_of(conn, "wix2.com")))
    check(f"{name}: a newer logo is not taken down by an old cleared URL",
          logo_of(conn, "moved.com") == "https://moved.com/newer-good-logo.png", repr(logo_of(conn, "moved.com")))
    check(f"{name}: real logos are still written", logo_of(conn, "acme.com") == "https://cdn/acme.png",
          repr(logo_of(conn, "acme.com")))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
