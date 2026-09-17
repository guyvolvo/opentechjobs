"""The companies the landing page's logo row puts first.

Big tech and well-known startups past their seed round. Nothing in the
data says which companies those are: ranked by open jobs alone, the row
put a yoga brand and a veterinary group next to Amazon. Funding stage
would need a paid source like Crunchbase, so this is picked by hand.

Domains as the board stores them (andurilindustries.com, not
anduril.com). A domain with no open jobs is skipped, so listing a company
before its board is scraped does no harm. Checked against the board's
companies on 2026-09-14; meta.com is left out because its only "job" is
a Recruitee false positive.
"""

HOT_COMPANIES = frozenset({
    # Big tech
    "amazon.com", "aws.amazon.com", "apple.com", "microsoft.com", "google.com",
    "nvidia.com", "salesforce.com", "paloaltonetworks.com", "crowdstrike.com",
    # AI
    "anthropic.com", "openai.com", "cohere.com", "cursor.com", "harvey.ai",
    "elevenlabs.io", "sierra.ai", "decagon.ai", "replit.com", "supabase.com",
    "abridge.com", "tenstorrent.com", "wayve.ai",
    # Infrastructure and developer tools
    "databricks.com", "snowflake.com", "cloudflare.com", "datadog.com",
    "mongodb.com", "elastic.co", "gitlab.com", "nebius.com", "crusoe.com",
    "coreweave.com", "confluent.io", "posthog.com", "twilio.com",
    "zscaler.com", "wiz.io", "snyk.io",
    # Fintech
    "stripe.com", "ramp.com", "brex.com", "plaid.com", "coinbase.com",
    "robinhood.com", "mercury.com", "chime.com", "affirm.com", "monzo.com",
    "wise.com",
    # Product
    "figma.com", "notion.com", "airbnb.com", "reddit.com", "discord.com",
    "dropbox.com", "instacart.com", "duolingo.com", "canva.com",
    "airtable.com", "asana.com", "intercom.com", "samsara.com", "gusto.com",
    # Space and defence
    "spacex.com", "andurilindustries.com", "palantir.com",
    # Israel
    "monday.com", "mobileye.com", "fireblocks.com", "catonetworks.com",
    "taboola.com", "similarweb.com", "appsflyer.com", "payoneer.com",
    "jfrog.com", "axonius.com", "riskified.com", "lemonade.com", "via.com",
    "checkpoint.com", "cyberark.com",
})

# Where a picked company's resolved logo is missing or poor, the image to
# use instead. Apple has no resolved logo and Google's favicon service
# only has a 64px one; its own touch icon is 152px.
LOGO_PINS = {
    "apple.com": "https://www.apple.com/apple-touch-icon.png",
}

# The resolver's own last step (company_logo.py). A picked company with no
# resolved logo gets this: every company on the list is big enough for
# Google to hold a real icon, checked for Microsoft and Google at 128px.
GOOGLE_FAVICON = "https://www.google.com/s2/favicons?domain={domain}&sz=128"
