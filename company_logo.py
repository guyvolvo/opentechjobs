"""Find a company's real logo, once, on the server.

The board used to guess this in the browser on every page view: try
{domain}/apple-touch-icon.png, then {domain}/favicon.ico, then Google's
favicon service, then give up and draw a lettered square. Three things
were wrong with that.

The domain is a guess. Discovery derives it from the ATS tenant slug
when the company's own site is not reachable, so SentinelOne was stored
as sentinellabs.io and Gong as gongio.com, a domain nobody owns. Asking
those for a favicon was never going to work, and an audit of the 124
companies with open Israeli listings found 14 falling through to the
lettered square, most of them for exactly this reason.

Guessing two fixed paths misses sites that declare their icon anywhere
else, which is most of them. A <link rel="icon"> can point at any URL,
and frequently does.

And a wrong answer was indistinguishable from a right one. Google's
service answers 200 with a generic globe for domains it has never seen,
and once returned an unrelated 98x53 image for doitintl.com, so a real
company's listing carried a stranger's logo.

The fix is to ask the ATS first. A company uploads its own logo when it
sets up its job board, the ATS serves it from a CDN, and the token we
use to fetch that company's jobs is the one identifier we know is
correct. Every ATS here except JazzHR and Workday exposes it.

Order, best first:

  1. the ATS's own copy, keyed on the token
  2. whatever the company's site declares in <link rel="...icon">
  3. the site's og:image
  4. Google's favicon service, rejected if it returns its 16x16 placeholder
  5. nothing, and the caller draws a lettered square

Every candidate is fetched and checked before it is accepted, so a
soft-404 HTML page served as apple-touch-icon.png cannot win.
"""

import re
import sys
from html.parser import HTMLParser

import requests

TIMEOUT = 12
# Below this a "logo" is a spacer, a tracking pixel, or an error page
# that happens to sniff as an image.
MIN_BYTES = 200
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Verification has to ask the way the board will ask, which means
# sending our own Referer. Several sites hotlink-protect their assets:
# www.axon.com/favicon/apple-touch-icon.png answers 200 to a bare
# request and 403 to the same request with a cross-origin referer, so it
# passed verification here and then failed in every visitor's browser,
# which fell through to a lettered square. Found live from a screenshot
# where the list showed a monogram and the detail drawer, which had no
# resolved URL and so guessed, showed the real logo.
ORIGIN = "https://opentechjobs.org"

# Google answers 200 for a domain it has nothing for, with a generic
# globe that is always exactly this size whatever sz you ask for.
GOOGLE_PLACEHOLDER = (16, 16)


def _is_image(resp) -> bool:
    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    return ctype.startswith("image/")


def _png_size(body: bytes):
    if len(body) > 24 and body[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big")
    return None


def check_image(sess, url: str) -> bool:
    """Does this URL actually serve a usable image right now?

    Not a HEAD: several of these hosts answer HEAD differently from GET,
    and the soft-404 case only shows up in the body's content type.
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        r = sess.get(url, timeout=TIMEOUT, allow_redirects=True,
                     headers={"Referer": ORIGIN + "/", "Accept": "image/avif,image/webp,*/*"})
    except requests.RequestException:
        return False
    if r.status_code != 200 or not _is_image(r) or len(r.content) < MIN_BYTES:
        return False
    if _png_size(r.content) == GOOGLE_PLACEHOLDER and "s2/favicons" in url:
        return False
    return True


class _IconParser(HTMLParser):
    """Icons and og:image declared in a page's own head.

    rel is a space-separated token list, so "shortcut icon" and
    "apple-touch-icon-precomposed" both have to match.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.icons: list[tuple[int, str]] = []
        self.og: str | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "link":
            rels = a.get("rel", "").lower().split()
            href = a.get("href", "").strip()
            if not href:
                return
            if any("apple-touch-icon" in r for r in rels):
                # Preferred: it exists to be shown at size, so it is a
                # real logo rather than a 16x16 glyph.
                self.icons.append((0, href))
            elif "icon" in rels or "shortcut" in rels or "mask-icon" in rels:
                self.icons.append((1, href))
        elif tag == "meta":
            if a.get("property", "").lower() == "og:image" and a.get("content"):
                self.og = self.og or a["content"].strip()


def _absolute(base: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return base.rstrip("/") + "/" + href


def site_icons(sess, domain: str) -> list[str]:
    """Icon URLs a site declares, best first, then the two fixed guesses.

    The declared ones come first because they are the only ones the site
    actually claims. The guesses stay as a tail for sites that serve the
    files but never reference them.
    """
    if not domain:
        return []
    base = f"https://{domain}"
    out: list[str] = []
    try:
        r = sess.get(base + "/", timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and "html" in r.headers.get("Content-Type", "").lower():
            # r.url, not base: a site that redirects to www or to another
            # domain declares its icons relative to where it landed.
            landed = re.sub(r"(https?://[^/]+).*", r"\1", r.url)
            p = _IconParser()
            p.feed(r.text[:400_000])
            for _, href in sorted(p.icons, key=lambda t: t[0]):
                out.append(_absolute(landed, href))
            if p.og:
                out.append(_absolute(landed, p.og))
    except (requests.RequestException, ValueError):
        pass
    out += [base + "/apple-touch-icon.png", base + "/favicon.ico"]
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


# Where each ATS keeps the logo its customer uploaded. Two shapes: a
# direct URL we can build from the token, and a page we have to read the
# URL out of. Confirmed against a live board each, 2026-09-11.
_ATS_LOGO_PAGE = {
    # s3-, s6- and friends: the CDN is sharded per account, so pinning a
    # single host silently missed companies (Gong is on s6). And a board
    # can carry a wide banner as well as a logo, with the banner first in
    # the markup, so match only the logos path rather than taking
    # whichever image appears first (doitintl's banner would have won).
    "greenhouse": ("https://job-boards.greenhouse.io/{token}",
                   re.compile(r"https://s\d+-recruiting\.cdn\.greenhouse\.io/"
                              r"external_greenhouse_job_boards/logos/[^\s\"'<>\\]+")),
    "lever": ("https://jobs.lever.co/{token}",
              re.compile(r"https://lever-client-logos\.s3[^\s\"'<>\\]+")),
    "ashby": ("https://jobs.ashbyhq.com/{token}",
              re.compile(r"https://app\.ashbyhq\.com/api/images/org-theme-logo/[^\s\"'<>\\]+")),
    "recruitee": ("https://{token}.recruitee.com/",
                  re.compile(r"https://careers\.recruiteecdn\.com/image/upload/[^\s\"'<>\\&]+")),
    "teamtailor": ("https://{token}.teamtailor.com/",
                   re.compile(r"https://images\.teamtailor-cdn\.com/images/s3/[^\s\"'<>\\]+")),
    # Personio's own brandmark is on every board, so match only the
    # per-customer path with a numeric account id in it.
    "personio": ("https://{token}.jobs.personio.de/",
                 re.compile(r"https://assets\.cdn\.personio\.de/logos/\d+/[^\s\"'<>\\]+")),
}


def ats_logo(sess, ats: str, token: str) -> str | None:
    """The logo the company uploaded to its own job board, if any.

    This is the best source available: the company chose the image, the
    ATS serves it from a CDN, and `token` is the identifier we already
    trust enough to fetch that company's jobs with. It is also immune to
    the stored domain being a guess.
    """
    if not ats or not token:
        return None

    if ats == "comeet":
        # token is "uid:secret"; the public logo endpoint wants the uid
        # and the board's slug, and accepts the uid in both positions.
        uid = token.split(":")[0]
        for size in ("original", "medium"):
            url = f"https://www.comeet.co/pub/{uid}/{uid}/logo?size={size}"
            if check_image(sess, url):
                return url
        return None

    if ats == "workable":
        # The widget endpoint we already poll does not carry it, but the
        # public search does, on the company object.
        try:
            r = sess.get("https://jobs.workable.com/api/v1/jobs",
                         params={"query": token, "limit": 5}, timeout=TIMEOUT)
            for job in (r.json().get("jobs") or []):
                company = job.get("company") or {}
                url = company.get("image")
                if url and token.lower() in (company.get("url") or "").lower():
                    if check_image(sess, url):
                        return url
        except (requests.RequestException, ValueError):
            pass
        return None

    spec = _ATS_LOGO_PAGE.get(ats)
    if not spec:
        return None
    page, pattern = spec
    try:
        r = sess.get(page.format(token=token), timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    for url in pattern.findall(r.text):
        # Boards inline the same logo at several sizes; the first is as
        # good as any and they all live on the same CDN.
        if check_image(sess, url):
            return url
    return None


def google_favicon(domain: str, size: int = 128) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz={size}"


def resolve_logo(sess, domain: str, ats: str | None = None,
                 token: str | None = None) -> tuple[str | None, str]:
    """Best logo URL for a company, and which tier produced it.

    Returns (url, source). A None url means every tier failed and the
    caller should draw its own lettered square, which is a real answer
    for a company whose site genuinely serves no icon anywhere.
    """
    url = ats_logo(sess, ats or "", token or "")
    if url:
        return url, "ats"

    for candidate in site_icons(sess, domain):
        if check_image(sess, candidate):
            return candidate, "site"

    if domain:
        candidate = google_favicon(domain)
        if check_image(sess, candidate):
            return candidate, "google"

    return None, "none"


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    s = session()
    for arg in sys.argv[1:]:
        parts = arg.split(",")
        domain = parts[0]
        ats = parts[1] if len(parts) > 1 else None
        token = parts[2] if len(parts) > 2 else None
        url, source = resolve_logo(s, domain, ats, token)
        print(f"{domain:28} {source:8} {url or '-'}")
