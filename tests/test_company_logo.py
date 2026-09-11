"""Logo resolution: ask the ATS, then read the site, then guess.

The old path guessed two fixed URLs on a domain that is itself a guess,
so a company whose icon lived anywhere else, or whose stored domain was
a tenant slug nobody owns, got a lettered square. The tests that matter
here are the ones about not accepting a wrong answer: a soft-404 HTML
page served as apple-touch-icon.png, and Google's generic globe, both
arrive as a 200 and neither is a logo.

Run directly, no framework:  python tests/test_company_logo.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import company_logo as cl  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def png(w, h, pad=4000):
    return (PNG_HEADER
            + w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + b"\x00" * pad)


class Resp:
    def __init__(self, status=200, ctype="image/png", body=b"", url=""):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.content = body
        self.text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        self.url = url


class FakeSession:
    """Answers from a {url_substring: Resp} table; anything else 404s."""

    def __init__(self, table):
        self.table = table
        self.asked = []

    def get(self, url, timeout=None, allow_redirects=True, params=None):
        self.asked.append(url)
        for key, resp in self.table.items():
            if key in url:
                resp.url = resp.url or url
                return resp
        return Resp(404, "text/html", b"nope", url)


# check_image is the gate everything else depends on.
s = FakeSession({"good.png": Resp(200, "image/png", png(180, 180))})
check("a real image passes", cl.check_image(s, "https://x/good.png"))
check("a 404 does not", not cl.check_image(s, "https://x/missing.png"))

s = FakeSession({"soft.png": Resp(200, "text/html; charset=utf-8", b"<html>not found</html>" * 50)})
check("a soft-404 HTML page served as a .png does not",
      not cl.check_image(s, "https://x/soft.png"))

s = FakeSession({"tiny.png": Resp(200, "image/png", png(1, 1, pad=10))})
check("a near-empty response does not", not cl.check_image(s, "https://x/tiny.png"))

# Google answers 200 with a 16x16 globe for domains it has never seen.
s = FakeSession({"s2/favicons": Resp(200, "image/png", png(16, 16))})
check("Google's 16x16 placeholder is rejected",
      not cl.check_image(s, cl.google_favicon("nowhere.example")))
s = FakeSession({"s2/favicons": Resp(200, "image/png", png(64, 64))})
check("but a real Google favicon is kept",
      cl.check_image(s, cl.google_favicon("somewhere.example")))
# The same 16x16 from a company's own site is a real, if small, icon.
s = FakeSession({"own.ico": Resp(200, "image/x-icon", png(16, 16))})
check("a 16x16 from the site itself is still accepted",
      cl.check_image(s, "https://x/own.ico"))

# Reading what a site declares, which is the whole point of not guessing.
HTML = b"""<html><head>
  <link rel="shortcut icon" href="/static/fav.ico">
  <link rel="apple-touch-icon" href="https://cdn.example.com/touch.png">
  <meta property="og:image" content="//cdn.example.com/og.png">
</head><body></body></html>"""
s = FakeSession({"site.example/": Resp(200, "text/html", HTML, url="https://site.example/")})
icons = cl.site_icons(s, "site.example")
check("the apple-touch-icon is preferred over the shortcut icon",
      icons[0] == "https://cdn.example.com/touch.png", repr(icons[:2]))
check("a root-relative href becomes absolute",
      "https://site.example/static/fav.ico" in icons, repr(icons))
check("a protocol-relative og:image becomes https",
      "https://cdn.example.com/og.png" in icons, repr(icons))
check("and the two fixed guesses are still there, at the end",
      icons[-2:] == ["https://site.example/apple-touch-icon.png",
                     "https://site.example/favicon.ico"], repr(icons[-2:]))

# A site that declares nothing still gets the guesses.
s = FakeSession({"bare.example/": Resp(200, "text/html", b"<html></html>", url="https://bare.example/")})
check("a site declaring no icon falls back to guessing",
      cl.site_icons(s, "bare.example") == ["https://bare.example/apple-touch-icon.png",
                                           "https://bare.example/favicon.ico"])

# Redirects: icons are relative to where the request landed.
s = FakeSession({"redir.example": Resp(200, "text/html",
                                       b'<html><head><link rel="icon" href="/i.png"></head></html>',
                                       url="https://www.elsewhere.example/home")})
check("a redirect resolves icons against the final host",
      "https://www.elsewhere.example/i.png" in cl.site_icons(s, "redir.example"),
      repr(cl.site_icons(s, "redir.example")))

# The ATS tier. Greenhouse shards its CDN across s3-, s6- and others, and
# a board can carry a banner as well as a logo, with the banner first.
BOARD = (b'<html><img src="https://s3-recruiting.cdn.greenhouse.io/'
         b'job_board_renderer/job_board_configurations/banners/400/028/400/original/b.png">'
         b'<img src="https://s6-recruiting.cdn.greenhouse.io/'
         b'external_greenhouse_job_boards/logos/400/072/600/original/real.png"></html>')
s = FakeSession({
    "job-boards.greenhouse.io/acme": Resp(200, "text/html", BOARD),
    "logos/400/072/600": Resp(200, "image/png", png(400, 120)),
})
check("the Greenhouse logo is found on a non-s3 shard",
      cl.ats_logo(s, "greenhouse", "acme")
      == "https://s6-recruiting.cdn.greenhouse.io/external_greenhouse_job_boards/logos/400/072/600/original/real.png",
      repr(cl.ats_logo(s, "greenhouse", "acme")))
check("and the banner above it is not mistaken for one",
      "banners" not in (cl.ats_logo(s, "greenhouse", "acme") or ""))

s = FakeSession({"job-boards.greenhouse.io/empty": Resp(200, "text/html", b"<html></html>")})
check("a board with no uploaded logo returns nothing",
      cl.ats_logo(s, "greenhouse", "empty") is None)
check("an ATS we have no logo route for returns nothing",
      cl.ats_logo(FakeSession({}), "jazzhr", "whoever") is None)
check("and a missing token returns nothing",
      cl.ats_logo(FakeSession({}), "greenhouse", "") is None)

# Order: the ATS wins even when the site has a perfectly good icon,
# because the stored domain may not be this company's at all.
s = FakeSession({
    "job-boards.greenhouse.io/acme": Resp(200, "text/html", BOARD),
    "logos/400/072/600": Resp(200, "image/png", png(400, 120)),
    "wrongco.example/": Resp(200, "text/html", HTML, url="https://wrongco.example/"),
    "cdn.example.com/touch.png": Resp(200, "image/png", png(180, 180)),
})
url, source = cl.resolve_logo(s, "wrongco.example", "greenhouse", "acme")
check("the ATS logo beats the site's own icon", source == "ats", f"{source} {url}")

# With no ATS logo, the site is next, and Google is last.
s = FakeSession({
    "job-boards.greenhouse.io/acme": Resp(200, "text/html", b"<html></html>"),
    "site.example/": Resp(200, "text/html", HTML, url="https://site.example/"),
    "cdn.example.com/touch.png": Resp(200, "image/png", png(180, 180)),
    "s2/favicons": Resp(200, "image/png", png(64, 64)),
})
url, source = cl.resolve_logo(s, "site.example", "greenhouse", "acme")
check("the site's declared icon is next", source == "site" and "touch.png" in url,
      f"{source} {url}")

s = FakeSession({"s2/favicons": Resp(200, "image/png", png(64, 64))})
url, source = cl.resolve_logo(s, "nothing.example", "jazzhr", "x")
check("Google is the last resort before giving up", source == "google", f"{source} {url}")

s = FakeSession({"s2/favicons": Resp(200, "image/png", png(16, 16))})
url, source = cl.resolve_logo(s, "nothing.example", "jazzhr", "x")
check("and a company with no logo anywhere says so",
      url is None and source == "none", f"{source} {url}")

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
