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
  3. Google's favicon service, rejected if it returns its 16x16 placeholder
     or a parked domain's registrar icon (see PLACEHOLDER_ICONS)
  4. nothing, and the caller draws a lettered square

Not og:image. It looks tempting and is always the wrong shape: a social
share card, 1200x630, which in a square slot is a squashed strip.

Every candidate is fetched and checked before it is accepted, so a
soft-404 HTML page served as apple-touch-icon.png cannot win.
"""

import hashlib
import re
import sys
from pathlib import Path
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
ORIGIN = "https://oceanofjobs.com"

# Google answers 200 for a domain it has nothing for, with a generic
# globe that is always exactly this size whatever sz you ask for.
GOOGLE_PLACEHOLDER = (16, 16)

# Icons that are not any company's logo, however many companies end up
# showing them. Wix's listings showed GoDaddy's logo: its recorded domain,
# wix2.com (from its SmartRecruiters account name), is a parked GoDaddy
# domain, and Google answers a parked domain with the registrar's icon,
# full size and a perfectly valid image. Fingerprinting Google's favicon
# for all 4,206 companies with open listings (2026-09-14) found 101 more
# domains returning that exact image, and every icon shared by three or
# more unrelated domains turned out to be a parking page or a hosting or
# framework default. At least 25 companies were displaying one.
#
# The key is icon_fingerprint() of the body as Google serves it at sz=128.
PLACEHOLDER_ICONS = {
    "2dec4705e9ab399e": "Cloudflare's orange cloud on a parked domain (greeneking.org)",
    "c20af3aed3deab7c": "GoDaddy parked domain (102 domains)",
    "0e81a4f2798c5e8d": "parked domain, blue triangle (28)",
    "80dcf26cf6a6d55b": "parked domain, house (23)",
    "19ce073b22e247e3": "grey cube placeholder (19)",
    "4c175bcbfb18979b": "parked domain, green arrows (11)",
    "1b4b91c96c452abd": "parked domain, nP (10)",
    "fce576d17f7b2cbb": "WordPress default (7)",
    "48af32a99eee0f67": "parked domain, blue star (5)",
    "ad2b832bb245da24": "parked domain, b (5)",
    "93bb05c501df2dd1": "parked domain, orange D (4)",
    "a9849fc58e73d025": "React default (3)",
    "157aa89d1ad45eb6": "OVH hosting default (3)",
    "c8cdaf2f2680ecd3": "Vercel default (3)",
    "21699edc740c476f": "shared placeholder, loop (3)",
    "2245fb06a5d8654d": "Salesforce parked domain (3)",
    # Found 2026-09-21 by hashing all 3,843 site-sourced logos at once
    # rather than one at a time. 264 of them were an image some other
    # company was also using. These are the ones that are demonstrably
    # nobody's logo, biggest first; the count is how many companies were
    # wearing each.
    #
    # Two of the clusters are NOT here on purpose. A six-domain and a
    # five-domain group turned out to be real logos, belonging to
    # outfits that run several sites, and a shared logo is the correct
    # answer for those.
    "ec607eb417e0a18a": "GoDaddy site builder default, house (54)",
    "970c2da77af56554": "Spaceship parked domain, triangle (46)",
    "292d9aeb457ab7fe": "blank white 16x16 (16)",
    "ae3e0f8e856e05a6": "parked domain, orange D, large variant (13)",
    "c3532d39f298c863": "WordPress default, grey (9)",
    "f9b98389969798b6": "DomainMarket parked domain (7)",
    "bd298981e6d8d7e4": "Sedo parked domain (7)",
    "d73d2f6d74ec6cdc": "WordPress default, blue (5)",
    "b0c679def36e3ccb": "parked domain, orange b (5)",
    "bddfab731b076fe0": "hosting default, teal shield (5)",
}

# The same placeholders, recognised by where they are served from.
#
# A fingerprint only catches the exact bytes it was taught. There were
# already a GoDaddy icon, an orange D and a WordPress default in the set
# above, and all three turned up again as different sizes of the same
# picture, wearing hashes nobody had seen. A parking service hands out
# whatever size the page asks for, so hashes alone lose that race
# forever.
#
# These are paths that cannot belong to a company: a domain-sale
# landing page's own branding, or a site builder's stock logo shipped
# with an empty template.
# The list lives in loader/placeholder_logos.py, shared with the loader,
# which strips the same URLs whichever path wrote them.
sys.path.insert(0, str(Path(__file__).resolve().parent / "loader"))
from placeholder_logos import PLACEHOLDER_URL_PARTS  # noqa: E402

PLACEHOLDER_URL_RE = re.compile("|".join(re.escape(p) for p in PLACEHOLDER_URL_PARTS), re.I)

# The same placeholders again, recognised by what they look like.
#
# Neither of the two rules above was enough on its own. The hashes only
# know the exact bytes they were taught, and the URL rule cannot help
# when the picture arrives from somewhere legitimate. Google's favicon
# service is the case that proved it: rejecting the WordPress default
# from a company's own site just moved every one of those companies onto
# the Google tier, which served the same mark at a third size, from a
# host that obviously cannot be blocked.
#
# So this compares the picture. Each value is an average hash: the image
# in greyscale at 8x8, one bit per pixel for brighter or darker than its
# own mean. Resizing and recompressing barely move it. The three
# WordPress variants that got through, at 80px, 180px and Google's
# 128px, sit within one bit of each other.
#
# The threshold is where it is because the measurements leave room for
# it. Every family below is at least 15 bits from every other, and the
# nearest real logo in the set that prompted this was 18 away.
PLACEHOLDER_SHAPES = {
    0xe7a5a51452cbc1e7: "WordPress default",
    0x003c7e66663c1800: "GoDaddy site builder default, a house",
    0xff008181c3c3e7ff: "Spaceship parked domain",
    0xc183831d1d8383c1: "parked domain, orange D",
    0xc1819f83c1f981c7: "Sedo parked domain",
    0xc7911646723c99c3: "parked domain, orange b",
    0xe7c3819db18181e7: "hosting default, teal shield",
}
SHAPE_DISTANCE = 6


def shape_fingerprint(body: bytes):
    """An 8x8 average hash, or None when this cannot be worked out.

    Pillow is imported here rather than at the top because probe.py
    imports this module and probe.py is bundled into the scrape Lambda,
    where an image library would be tens of megabytes to carry for a
    check the Lambda never performs. The resolver installs it; anything
    that does not simply falls back to the two exact rules above, which
    is how this behaved before shapes existed.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        import io
        im = Image.open(io.BytesIO(body))
        if im.mode in ("RGBA", "LA", "P"):
            # Over white, because that is what the board puts it on. A
            # white mark on transparency would otherwise read as blank.
            im = im.convert("RGBA")
            flat = Image.new("RGBA", im.size, (255, 255, 255, 255))
            flat.alpha_composite(im)
            im = flat
        im = im.convert("L").resize((8, 8), Image.LANCZOS)
        px = list(im.getdata())
        if len(px) != 64:
            return None
        avg = sum(px) / 64
        bits = 0
        for i, value in enumerate(px):
            if value > avg:
                bits |= 1 << i
        return bits
    except Exception:
        # A malformed or unsupported image is not a placeholder, it is
        # just one this cannot judge. The byte rules still apply.
        return None


def looks_like_placeholder(body: bytes):
    """Which placeholder this image is a version of, or None."""
    shape = shape_fingerprint(body)
    if shape is None:
        return None
    for known, label in PLACEHOLDER_SHAPES.items():
        if bin(shape ^ known).count("1") <= SHAPE_DISTANCE:
            return label
    return None


def icon_fingerprint(body: bytes) -> str:
    return hashlib.sha1(body).hexdigest()[:16]


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
    if icon_fingerprint(r.content) in PLACEHOLDER_ICONS:
        return False
    # Checked against the URL that actually served the bytes, not the one
    # we asked for: a parked domain's own favicon.ico is a redirect to
    # the parking service, and the redirect is where the tell is.
    if PLACEHOLDER_URL_RE.search(r.url or url):
        return False
    if looks_like_placeholder(r.content):
        return False
    return True


class _IconParser(HTMLParser):
    """The icons a page declares in its own head.

    rel is a space-separated token list, so "shortcut icon" and
    "apple-touch-icon-precomposed" both have to match.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.icons: list[tuple[int, str]] = []

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
        # og:image is deliberately not collected. It is a social share
        # card, not a logo: NVIDIA's is nvidia-corporate-og-image-1200x630
        # .jpg, which rendered in a square slot as a squashed green strip.
        # Reported live from a screenshot of exactly that.


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


# How many site candidates to weigh before settling.
#
# The list is already best-first, so this is not a search, it is a
# tie-break: keep looking a little past the first thing that works in
# case a later one is a mark on transparency rather than a painted
# square. Four covers the declared icons plus the two fixed guesses on
# almost every site, and the loop stops early the moment it finds one.
TRANSPARENCY_LOOKAHEAD = 4


def has_transparency(body: bytes) -> bool:
    """A real see-through area, not just an alpha channel that is full.

    Used to prefer a mark over a painted tile. Pillow is imported here,
    not at the top: probe.py pulls this module into the scrape Lambda,
    where an image library is tens of megabytes for a check that never
    runs there. Without it every candidate answers False and the first
    working one wins, which is how this behaved before.
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        import io
        im = Image.open(io.BytesIO(body))
        if im.mode not in ("RGBA", "LA", "P"):
            return False
        im = im.convert("RGBA")
        alpha = im.getchannel("A")
        if alpha.getextrema()[0] >= 250:
            return False
        clear = sum(1 for v in alpha.tobytes() if v < 16)
        # Five percent, so a rounded corner's few soft pixels do not
        # count as a transparent background.
        return clear / float(im.width * im.height) > 0.05
    except Exception:
        return False


def resolve_logo(sess, domain: str, ats: str | None = None,
                 token: str | None = None) -> tuple[str | None, str]:
    """Best logo URL for a company, and which tier produced it.

    Returns (url, source). A None url means every tier failed and the
    caller should draw its own lettered square, which is a real answer
    for a company whose site genuinely serves no icon anywhere.

    Among the site's own icons, one with a transparent background wins
    over one without, even if the opaque one came first. A favicon is
    usually a painted square; a mark on transparency sits on any ground
    and can be tinted. Measured 2026-09-22 over the thirty companies in
    the landing page's logo band: five of the fourteen opaque ones had a
    transparent alternative the old first-match rule was walking past.
    """
    url = ats_logo(sess, ats or "", token or "")
    if url:
        return url, "ats"

    first_working = None
    for candidate in site_icons(sess, domain)[:TRANSPARENCY_LOOKAHEAD]:
        if not check_image(sess, candidate):
            continue
        if first_working is None:
            first_working = candidate
        try:
            body = sess.get(candidate, timeout=TIMEOUT, allow_redirects=True,
                            headers={"Referer": ORIGIN + "/"}).content
        except requests.RequestException:
            continue
        if has_transparency(body):
            return candidate, "site"
    if first_working:
        return first_working, "site"

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
