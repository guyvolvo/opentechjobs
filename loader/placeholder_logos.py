"""Logo URLs that stand for no company at all.

Placeholders a parked domain, a site builder or an ATS hands out when a
company has not set a picture of its own. Each one was found on the live
board, shared by companies with nothing in common (counts as of
2026-09-26): Recruitee's own share image on 308 companies, GoDaddy's
default on 51, Spaceship's "for sale" icon on 40, Wix's default on 15.

Read by company_logo.py, which refuses them when resolving, and by the
loader, which strips them on every apply wherever they came from:
discovery writes logo URLs through its own upsert, so the resolver's
filter alone left GoDaddy's icon on 51 companies after it was listed.

Substrings, not a regex, so the loader can match them with LIKE.
"""

PLACEHOLDER_URL_PARTS = (
    # parked domains and registrars
    "sedoparking.com",
    "cdn.domainmarket.com",
    "forsale.spaceship-cdn.com",
    "parkingcrew.net",
    "afternic.com",
    "bodis.com",
    "ts.domainname.de",
    # site builders' defaults
    "wsimg.com/isteam/ip/static/pwa-app/logo-default",
    "static.parastorage.com/client/pfavico",
    "www.wix.com/favicon.ico",
    "assets.superlander.com/newimages/webclip",
    "gstatic.com/images/branding/productlogos/sites_",
    "s1.wp.com/i/favicon.ico",
    "dovendi.b-cdn.net/src/assets/favicon",
    "mkt-static.bitly.com",
    # an ATS's own branding, not the employer's
    "recruiteecdn.com/image/upload/q_auto,w_1920,c_limit/assets/tellent-share",
    # Cloudflare's, which is nobody's employer here
    "www.cloudflare.com/favicon.ico",
    "favicon.im/dash.cloudflare.com",
)

# Written in place of a placeholder: "there is no logo". Not NULL, which
# the browser reads as "work one out", and working one out on a parked
# domain fetches the same placeholder straight back.
NO_LOGO = ""


def is_placeholder(url: str | None) -> bool:
    return bool(url) and any(part in url for part in PLACEHOLDER_URL_PARTS)
