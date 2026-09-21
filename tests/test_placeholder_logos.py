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

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
