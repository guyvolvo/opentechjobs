"""A logo that several unrelated companies share is nobody's logo.

Reported live 2026-09-21: lots of listings were showing the WordPress
icon. Hashing all 3,843 site-sourced logos at once, rather than judging
them one at a time as they were fetched, found 264 companies wearing an
image that belonged to somebody else. Two of those clusters were the
WordPress default in grey and in blue, 14 companies between them, and
the blocklist already had a WordPress entry. It had learned one size of
the picture and never saw the others.

So this file pins two things. The exact images found that day, and the
rule that catches the next size of them without anyone hashing anything.

Run directly, no framework:  python tests/test_placeholder_logos.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import company_logo  # noqa: E402
import resolve_company_logos as rcl  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


# The clusters found live, by fingerprint, with how many companies wore
# each one. If a future edit drops one of these, these listings quietly
# go back to showing somebody else's brand.
FOUND = {
    "ec607eb417e0a18a": 54,   # GoDaddy site builder default, a house
    "970c2da77af56554": 46,   # Spaceship parked domain
    "292d9aeb457ab7fe": 16,   # a blank white square
    "ae3e0f8e856e05a6": 13,   # parked domain, orange D
    "c3532d39f298c863": 9,    # WordPress default, grey
    "f9b98389969798b6": 7,    # DomainMarket parked domain
    "bd298981e6d8d7e4": 7,    # Sedo parked domain
    "d73d2f6d74ec6cdc": 5,    # WordPress default, blue
    "b0c679def36e3ccb": 5,    # parked domain, orange b
    "bddfab731b076fe0": 5,    # hosting default, teal shield
}
missing = [f for f in FOUND if f not in company_logo.PLACEHOLDER_ICONS]
check("every placeholder found live is still blocked", not missing, repr(missing))
check("that covers the 167 companies they were shown on",
      sum(FOUND.values()) == 167, repr(sum(FOUND.values())))

# The two clusters deliberately left alone. Several domains run by one
# outfit share a logo legitimately, and blocking those would replace a
# correct logo with a letter.
for real in ("0bd160368d2a36af", "b38bb26b7ce444b5"):
    check(f"a real shared logo ({real[:8]}) is not blocked",
          real not in company_logo.PLACEHOLDER_ICONS)

# The URL rule, which is what makes this stop being whack-a-mole. A
# parking service serves whatever size the page asks for, so the bytes
# change and the host does not.
BLOCKED_URLS = [
    "https://img.sedoparking.com/templates/logos/sedo_logo.png",
    "https://cdn.domainmarket.com/dm-com/images/favicons/favicon-180x180.svg",
    "https://forsale.spaceship-cdn.com/static/latest/5.latest/assets/fonts/apple-touch-icon-57x57.png",
    "https://img1.wsimg.com/isteam/ip/static/pwa-app/logo-default.png/:/rs=w:57,h:57,m",
    "http://parkingcrew.net/favicon.ico",
]
for u in BLOCKED_URLS:
    check(f"served from a parking page, so it is not a logo: {u.split('/')[2]}",
          bool(company_logo.PLACEHOLDER_URL_RE.search(u)))

# And the part that matters more: a real company's favicon must survive.
REAL_URLS = [
    "https://wiz.io/favicon.ico",
    "https://www.monday.com/apple-touch-icon.png",
    "https://app.ashbyhq.com/api/images/org-theme-logo/1fd9/b904/7f9f.png",
    "https://workablehr.s3.amazonaws.com/uploads/account/logo/102300/logo",
    # Carries a blocked host's name inside its own path, which must not
    # be enough: the rule matches a host, not a substring anywhere.
    "https://realcompany.com/img/afternic-case-study.png",
    "https://realcompany.com/assets/bodis-comparison.svg",
]
for u in REAL_URLS:
    check(f"a real logo is left alone: {u.split('/')[2]}",
          company_logo.PLACEHOLDER_URL_RE.search(u) is None,
          repr(u))

# The shape check, which is the part that ends the game rather than
# playing another round of it. Rejecting the WordPress default from a
# company's own site just moved those companies onto Google's favicon
# service, which served the identical mark at a third size from a host
# nobody can block. Measured that day: the three variants are within one
# bit of each other, every placeholder family is at least 15 bits from
# every other, and the nearest real logo was 18 away.
WP_VARIANTS = [0xe7a5a51452cbc1e7, 0xe7a5a51452dbc1e7]  # grey 80px, blue 180px
for h in WP_VARIANTS:
    near = any(bin(h ^ k).count("1") <= company_logo.SHAPE_DISTANCE
               for k in company_logo.PLACEHOLDER_SHAPES)
    check(f"a WordPress default is recognised at any size ({h:#x})", near)

families = list(company_logo.PLACEHOLDER_SHAPES)
closest = min(bin(x ^ y).count("1")
              for i, x in enumerate(families) for y in families[i + 1:])
check("the families stay far enough apart to tell apart",
      closest > company_logo.SHAPE_DISTANCE * 2, f"closest pair is {closest} bits")
check("and the threshold is not wide enough to swallow a real logo",
      company_logo.SHAPE_DISTANCE <= 8, repr(company_logo.SHAPE_DISTANCE))
check("something that is not an image is judged by the byte rules, not rejected",
      company_logo.looks_like_placeholder(b"not an image at all") is None)
check("an empty body is not mistaken for a placeholder",
      company_logo.looks_like_placeholder(b"") is None)

# Rechecking. Version 2 only revisited Google-tier logos, which is why
# thousands of site favicons kept a placeholder the blocklist already
# knew about.
check("the check version moved, or nothing gets looked at again",
      rcl.LOGO_CHECK_VERSION >= 3, repr(rcl.LOGO_CHECK_VERSION))
old_site = {"url": "https://x.com/favicon.ico", "source": "site", "check": 2, "looked_at": "x.com"}
old_google = {"url": "https://x.com/g.png", "source": "google", "check": 2, "looked_at": "x.com"}
old_ats = {"url": "https://ashby/x.png", "source": "ats", "check": 2, "looked_at": "x.com"}
check("a site logo stored under the old rules is looked at again",
      rcl.needs_recheck("x.com", old_site))
check("so is a Google one, as before", rcl.needs_recheck("x.com", old_google))
check("an ATS logo is left alone: it comes from the company's own account",
      not rcl.needs_recheck("x.com", old_ats))
check("a logo already checked under the current rules is not re-fetched",
      not rcl.needs_recheck("x.com", dict(old_site, check=rcl.LOGO_CHECK_VERSION)))
check("a company with no logo at all is not a recheck candidate",
      not rcl.needs_recheck("x.com", {"source": "site", "check": 1}))

# Preferring a mark over a painted square. A favicon is usually a tile
# with the logo painted on it, and a tile cannot be tinted or sat on a
# coloured ground. Measured over the thirty companies in the landing
# band: this took transparent sources from 14 to 17.
PNG_OPAQUE = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d29dc"
    "0000000e49444154789c63f8cf80130c0c000401ff7f9d2e3d2a0000000049454e44ae426082")
check("an image with no alpha channel is not preferred",
      company_logo.has_transparency(PNG_OPAQUE) is False)
check("nor is something that is not an image at all",
      company_logo.has_transparency(b"<html>nope</html>") is False)
check("nor is an empty body", company_logo.has_transparency(b"") is False)
check("the lookahead is bounded, or one company could cost a dozen fetches",
      1 < company_logo.TRANSPARENCY_LOOKAHEAD <= 6, repr(company_logo.TRANSPARENCY_LOOKAHEAD))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
