"""Referral boards, and the companies behind them.

A company's private Greenhouse board for roles it does not post
publicly. Two problems come with one: the board calls itself "Referral
Board", which is not a company, and the domain is one discovery invented
from the board token, so referralsuseonly.com is not a website belonging
to anyone.

Both were reported live: a listing showed the referral board as its
employer, and clicking Apply revealed Elastic. The listings are real and
worth carrying, so the answer is to name them correctly rather than drop
them.

Identified from the job descriptions themselves, which are written by
the company and say who they are: 176 mentions of Elastic across the
first board, and sproutsocial.com in the second. The suffix stays on the
name because these roles genuinely are referral-only, and someone
deciding whether to apply should know that before they click.
"""

REFERRAL_BOARDS = {
    "referralsuseonly.com": {"name": "Elastic (referrals)", "logo_domain": "elastic.co"},
    "ssreferrals.com": {"name": "Sprout Social (referrals)", "logo_domain": "sproutsocial.com"},
}
