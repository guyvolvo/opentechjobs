"""Locations, which arrive as free text and have to come back as countries.

No ATS has a country field. Every board writes the location however it
likes, so countries.py reads "Chicago, IL", "München, de", "Tel
Aviv-Yafo, Tel Aviv, ISR" and "תל אביב" and has to answer with one code
each. The order of its rules is the whole design, and these tests exist
to hold that order in place while people keep adding cities to it.

The case that matters more than all the others put together is IL. On a
board whose entire premise is Israeli listings, a resolver that reads
bare uppercase two-letter codes as countries turns every Chicago job
into an Israeli one. In the 1,500 real locations the module was built
from, every uppercase IL was Illinois and not one was Israel. So the US
state rule fires last, after the city has had its say, and the tests
below pin both halves: Chicago stays American, Tel Aviv stays Israeli,
and a long list of plainly foreign places is checked for any trace of IL.

The rest is duplication. The board facets on these values, so one city
spelled four ways has to arrive as one option and not four, and a job
with four offices in Israel has to say IL once.

Run directly, no framework:  python tests/test_countries.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

import countries  # noqa: E402
from countries import (  # noqa: E402
    ALPHA2,
    CITIES,
    CITY_ALIASES,
    COUNTRY_LABELS,
    cities_of,
    city_string,
    countries_of,
    country_string,
    label_for,
)

# This file prints Hebrew, and a Windows console defaults to cp1252,
# where printing it raises UnicodeEncodeError and the run dies partway
# through with no report. Nothing to do with what is being tested.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


def countries_are(loc, expected):
    got = countries_of(loc)
    check("%r is %s" % (loc, ",".join(expected) or "no country"),
          got == expected, repr(got))


def cities_are(loc, expected):
    got = cities_of(loc)
    check("%r is %s" % (loc, ",".join(expected) or "no city"),
          got == expected, repr(got))


# The Illinois trap.
# "Chicago, IL" resolving to Israel is the failure this module was
# written to prevent, and it is a quiet one: the job still appears, just
# on the wrong filter, under the one label this board is judged on.
countries_are("Chicago, IL", ["US"])
countries_are("Tel Aviv, IL", ["IL"])

# Neither of those needs the state rule at all, because the city is read
# first. These do need it: the city is one nobody has heard of, so the
# only signal left in the string is the state code.
for loc in ["Springfield, IL", "Naperville, IL", "Schaumburg, IL",
            "Peoria, IL, United States", "Deerfield, IL, US",
            "Northbrook, IL, USA", "Chicago, IL (Hybrid)"]:
    check("%r is US, not Israel" % loc, countries_of(loc) == ["US"],
          repr(countries_of(loc)))

# A bare IL with nothing else in the string. Counted over the real data
# this is Chicago every single time, so US is the honest answer even
# though it is the one that looks wrong out of context.
countries_are("IL", ["US"])
countries_are("Illinois", ["US"])
countries_are("Remote - Illinois", ["US"])

# The other state codes that are also country codes. CO is Colombia, ME
# is Montenegro, DE is Germany, IN is India, and every one of them is a
# US state in this data.
for loc, code in [("Denver, CO", "US"), ("Portland, ME", "US"),
                  ("Wilmington, DE", "US"), ("Indianapolis, IN", "US"),
                  ("Boston, MA", "US"), ("Dallas, TX", "US"),
                  ("New York, NY", "US"), ("Seattle, WA", "US"),
                  ("Washington, DC", "US"), ("Atlanta, GA", "US")]:
    check("%r is %s" % (loc, code), countries_of(loc) == [code],
          repr(countries_of(loc)))

# Word boundaries, or "india" matches "indiana" and "indianapolis" and
# the state rule never gets a chance.
check("India is not read out of Indianapolis", "IN" not in countries_of("Indianapolis, IN"),
      repr(countries_of("Indianapolis, IN")))
check("Chile is not read out of Chicago", countries_of("Chicago, IL") == ["US"],
      repr(countries_of("Chicago, IL")))

# Spelled-out states, which is how some boards write a US address with
# no country anywhere in the string.
countries_are("Lehi, Utah", ["US"])
countries_are("Berthoud, Colorado", ["US"])
countries_are("Chicago, Illinois", ["US"])

# A city written before the state wins over the state, whichever side of
# the string it sits on.
countries_are("IL - Tel Aviv", ["IL"])
countries_are("Israel - Tel Aviv", ["IL"])


# Lowercase two-letter codes.
# One ATS writes them this way and they are trustworthy precisely
# because they are lowercase: nobody writes a US state in lowercase.
countries_are("München, de", ["DE"])
countries_are("Sofia, bg", ["BG"])
countries_are("Wolverhampton, gb", ["GB"])
countries_are("Warsaw, pl", ["PL"])
countries_are("Tel Aviv, il", ["IL"])

# Only real codes, so a two-letter word sitting in a location does not
# invent a country out of nothing.
countries_are("Remote, hi", [])


# Alpha-3 is an allowlist, not a computed set.
# CUN, SEA and NYC are airport codes with the exact shape of a country
# code, and boards paste them into locations constantly.
countries_are("Tel Aviv-Yafo, Tel Aviv, ISR", ["IL"])
countries_are("Cancun / CUN / Mexico", ["MX"])
countries_are("Seattle, WA / SEA", ["US"])
check("a bare airport code is no country at all",
      countries_of("CUN") == [] and countries_of("SEA") == [],
      repr((countries_of("CUN"), countries_of("SEA"))))
check("NYC answers US because it is a known city, not because it parsed as a code",
      countries_of("NYC") == ["US"] and "NYC" not in countries.COUNTRY_ALPHA3,
      repr(countries_of("NYC")))
check("no airport code is in the alpha-3 table",
      not [a for a in ("CUN", "SEA", "NYC", "LAX", "TLV", "SFO", "ORD")
           if a in countries.COUNTRY_ALPHA3],
      repr([a for a in ("CUN", "SEA", "NYC", "LAX", "TLV", "SFO", "ORD")
            if a in countries.COUNTRY_ALPHA3]))


# Country names and the aliases people actually type.
countries_are("Remote, United Kingdom", ["GB"])
countries_are("Remote - France", ["FR"])
countries_are("All France (remote)", ["FR"])
countries_are("US Remote", ["US"])
countries_are("Ukraine Office", ["UA"])
countries_are("Remote (Israel)", ["IL"])
countries_are("State of Israel", ["IL"])
countries_are("Cambridge, UK", ["GB"])
countries_are("Dubai, UAE", ["AE"])
countries_are("Istanbul, Türkiye", ["TR"])

# A city with a qualifier stuck to it, which is how the Israeli offices
# are usually written.
countries_are("Herzliya Pituach Office", ["IL"])


# Several locations on one job.
countries_are("Remote, Canada; Remote, Israel; Remote, United Kingdom",
              ["CA", "IL", "GB"])
countries_are("Bogota / CUN / Colombia; Mexico City / CDMX / Mexico",
              ["CO", "MX"])

# A job listing the world gets cut off rather than filling the filter.
many = "Israel; United States; Canada; United Kingdom; France; Germany; Spain; Italy; Poland; Brazil"
check("a long list is capped", len(countries_of(many)) == countries.MAX_COUNTRIES,
      repr(countries_of(many)))
check("and the cap keeps the ones written first",
      countries_of(many)[:3] == ["IL", "US", "CA"], repr(countries_of(many)))


# No duplicates, which is the whole point of a facet.
# A job with four Israeli offices is one IL in the filter, and a
# location naming one country three ways is still one country.
countries_are("Tel Aviv, Israel; Herzliya, IL; Ramat Gan, ישראל; Haifa, ISR", ["IL"])
countries_are("Israel, Tel Aviv, IL, ISR", ["IL"])
countries_are("Remote, United States; New York, NY; Austin, TX; USA", ["US"])
cities_are("Tel Aviv, Israel; Tel Aviv-Yafo, ISR; tel-aviv", ["Tel Aviv"])
cities_are("Ramat-Gan; Ramat Gan", ["Ramat Gan"])
cities_are("Montréal, Canada; Montreal, Canada", ["Montreal"])

# Stated as a property rather than a handful of cases, because the
# duplicates that reach the board are always the input nobody thought of.
DEDUPE_CORPUS = [
    "Tel Aviv, Israel; Herzliya, IL; Ramat Gan, ישראל; Haifa, ISR",
    "Israel, Tel Aviv, IL, ISR",
    "Remote, United States; New York, NY; Austin, TX; USA",
    "Remote, Canada; Remote, Israel; Remote, United Kingdom",
    "Bogota / CUN / Colombia; Mexico City / CDMX / Mexico",
    "Tel Aviv; Tel Aviv-Yafo; tel-aviv; Tel Aviv, il",
    "München, Germany; Munich, DE; Munich, Germany",
    "Montréal, Canada; Montreal, Canada; Montreal",
    "Ramat-Gan; Ramat Gan; Ramat Gan, Israel",
    "London, UK; London, United Kingdom; London, GBR",
    "New York, NY; New York City, NY; NYC",
    "Boston, MA; Chicago, IL; Denver, CO; Austin, TX",
    "Israel; United States; Canada; United Kingdom; France",
    "Herzliya; Herzliya Pituach; Herzliya Pituach Office",
    "",
    "Remote",
]
dup_countries = [loc for loc in DEDUPE_CORPUS
                 if len(set(countries_of(loc))) != len(countries_of(loc))]
dup_cities = [loc for loc in DEDUPE_CORPUS
              if len(set(cities_of(loc))) != len(cities_of(loc))]
check("no location repeats a country", not dup_countries, repr(dup_countries))
check("no location repeats a city", not dup_cities, repr(dup_cities))


# Nothing is Israel unless it is Israeli.
# This is the most damaging thing the module can get wrong, so the list
# is long on purpose and every entry is somewhere plainly not Israel.
NOT_ISRAEL = [
    "Chicago, IL", "Springfield, IL", "Naperville, IL", "Schaumburg, IL",
    "Peoria, IL, United States", "Deerfield, IL, US", "Northbrook, IL, USA",
    "Chicago, Illinois", "Chicago, IL (Hybrid)", "Remote - Illinois", "IL",
    "Denver, CO", "Portland, ME", "Wilmington, DE", "Indianapolis, IN",
    "New York, NY", "Seattle, WA", "Boston, MA", "Dallas, TX",
    "Washington, DC", "Lehi, Utah", "Berthoud, Colorado", "US Remote",
    "London, United Kingdom", "Cambridge, UK", "Dublin, Ireland",
    "Milan, IT", "Warsaw, PL", "Prague", "Sofia, bg", "Wolverhampton, gb",
    "Munich, Germany", "Kyiv, Ukraine", "Istanbul, Türkiye",
    "Bangalore, India", "Tokyo, Japan", "Sydney, Australia", "Dubai, UAE",
    "Toronto, ON, Canada", "Vancouver, BC, Canada", "Remote, Canada",
    "Bogota / CUN / Colombia", "Mexico City / CDMX / Mexico",
    "São Paulo, Brazil", "Georgia", "CUN", "SEA", "NYC",
    "Remote", "Distributed, Global", "Hybrid", "Remote, EMEA",
]
strays = [(loc, countries_of(loc)) for loc in NOT_ISRAEL if "IL" in countries_of(loc)]
check("no foreign location resolves to Israel", not strays, repr(strays))

# The other direction, since a resolver that never says IL would also
# pass the check above.
for loc in ["Israel", "Remote, Israel", "Tel Aviv", "Herzliya",
            "Tel Aviv-Yafo, Tel Aviv, ISR", "Tel Aviv, IL", "ישראל", "תל אביב"]:
    check("%r is Israel" % loc, countries_of(loc) == ["IL"], repr(countries_of(loc)))


# Cities, which are the same problem one layer down.
# Faceting on the raw string offers Tel Aviv four times. These seven
# spellings are all taken from real board output and all mean one place.
for loc in ["Tel Aviv", "Tel Aviv, Israel", "Tel Aviv-Yafo, Tel Aviv District, Israel",
            "tel-aviv", "IL - Tel Aviv", "Israel - Tel Aviv", "Tel Aviv, il"]:
    check("%r is the city Tel Aviv" % loc, cities_of(loc) == ["Tel Aviv"],
          repr(cities_of(loc)))

# The hyphen and the accent, which each used to sit in the filter twice.
check("Ramat-Gan and Ramat Gan are one city",
      cities_of("Ramat-Gan") == cities_of("Ramat Gan") == ["Ramat Gan"],
      repr((cities_of("Ramat-Gan"), cities_of("Ramat Gan"))))
check("Montréal and Montreal are one city",
      cities_of("Montréal, Canada") == cities_of("Montreal, Canada") == ["Montreal"],
      repr((cities_of("Montréal, Canada"), cities_of("Montreal, Canada"))))
check("München and Munich are one city",
      cities_of("München, de") == cities_of("Munich, Germany") == ["Munich"],
      repr((cities_of("München, de"), cities_of("Munich, Germany"))))
check("Köln and Cologne are one city",
      cities_of("Köln, Germany") == cities_of("Cologne, Germany") == ["Cologne"],
      repr((cities_of("Köln, Germany"), cities_of("Cologne, Germany"))))

# The accented spelling has to carry the country as well, not only the
# label. "München" with no country beside it is a German job, and the
# gazetteer is the only thing in the string that can say so.
check("a bare München is Germany", countries_of("München") == ["DE"],
      repr(countries_of("München")))
check("a bare Köln is Germany", countries_of("Köln") == ["DE"],
      repr(countries_of("Köln")))

# The gazetteer is looked up with folded text, so an entry whose key
# still carries a hyphen or an accent has to survive folding or it can
# never be found. It used to be hand-written on each key and nine of
# them were wrong, which is what this test found; the tables are folded
# once at import now, so the invariant is that every raw key is
# reachable THROUGH that fold rather than that it was typed folded.
unreachable = sorted(k for k in CITIES if countries._norm(k) not in countries.CITIES_FOLDED)
check("every city key survives folding, or it can never be looked up",
      not unreachable, repr(unreachable))
unreachable_arrangements = sorted(
    k for k in countries._NOT_A_CITY if countries._norm(k) not in countries._NOT_A_CITY_FOLDED)
check("and so does every work-arrangement word", not unreachable_arrangements,
      repr(unreachable_arrangements))
# Folding has to be stable, or a table folded once at import and an
# input folded on every call could still disagree.
check("folding a folded key changes nothing",
      all(countries._norm(k) == k for k in countries.CITIES_FOLDED),
      repr([k for k in countries.CITIES_FOLDED if countries._norm(k) != k][:5]))

# The rest of the alias table, spot-checked on the spellings the Israeli
# boards actually use.
for loc, city in [("Petach Tikva", "Petah Tikva"), ("Beersheba", "Beer Sheva"),
                  ("Be'er Sheva", "Beer Sheva"), ("Ra'anana", "Raanana"),
                  ("Rosh Haayin", "Rosh HaAyin"), ("Herzliya Pituach", "Herzliya"),
                  ("New York City", "New York"), ("NYC", "New York"),
                  ("Washington, DC", "Washington"), ("Kraków, Poland", "Krakow"),
                  ("Bogotá, Colombia", "Bogota"), ("CDMX, Mexico", "Mexico City")]:
    check("%r is the city %s" % (loc, city), cities_of(loc) == [city],
          repr(cities_of(loc)))

# One city per location, the first one written, and no more than four
# locations' worth. A job posted to every office should not hand the
# filter a dropdown of its own.
cities_are("Boston, MA; Chicago, IL; Denver, CO; Austin, TX",
           ["Boston", "Chicago", "Denver", "Austin"])
check("the city list is capped",
      len(cities_of("Herzliya; Tel Aviv; Ramat Gan; Netanya; Haifa"))
      == countries.MAX_CITIES,
      repr(cities_of("Herzliya; Tel Aviv; Ramat Gan; Netanya; Haifa")))

# A city nobody has heard of still appears, rather than vanishing from
# the filter because it is not in the gazetteer.
cities_are("Schaumburg, IL", ["Schaumburg"])
cities_are("Berthoud, Colorado", ["Berthoud"])

# A country is a country, not a city, however much Georgia might want to
# be both.
cities_are("Israel", [])
cities_are("Remote, United Kingdom", [])
cities_are("Georgia", [])


# Hebrew, because Niloosoft and the Israeli boards post in it.
# An unmatched Hebrew string used to mean no city and no country at all,
# on exactly the listings this board exists for.
for loc, city in [("תל אביב", "Tel Aviv"), ("תל־אביב", "Tel Aviv"),
                  ("ירושלים", "Jerusalem"), ("חיפה", "Haifa"),
                  ("הרצליה", "Herzliya"), ("רעננה", "Raanana"),
                  ("פתח תקווה", "Petah Tikva"), ("נתניה", "Netanya"),
                  ("רחובות", "Rehovot"), ("רמת גן", "Ramat Gan"),
                  ("באר שבע", "Beer Sheva"), ("אשדוד", "Ashdod"),
                  ("חולון", "Holon"), ("כפר סבא", "Kfar Saba")]:
    got = (countries_of(loc), cities_of(loc))
    check("%s is IL and the city %s" % (loc, city),
          got == (["IL"], [city]), repr(got))

# The regional labels are regions, not cities, and are kept as
# themselves rather than forced into a nearby city they are not.
for loc, city in [("אזור המרכז", "Central District"), ("אזור השפלה", "Shfela"),
                  ("אזור השרון", "Sharon"), ("אזור הצפון", "Northern District"),
                  ("אזור הדרום", "Southern District"),
                  ("ירושלים והסביבה", "Jerusalem")]:
    got = (countries_of(loc), cities_of(loc))
    check("%s is IL and the region %s" % (loc, city),
          got == (["IL"], [city]), repr(got))

# Hebrew for the country itself is a country. It must not become a city
# called ישראל sitting at the top of the city filter.
for loc in ["ישראל", "מדינת ישראל"]:
    got = (countries_of(loc), cities_of(loc))
    check("%s is the country with no city" % loc, got == (["IL"], []), repr(got))

# Mixed strings, which is what the boards actually send.
check("Hebrew city with Hebrew country is one city",
      (countries_of("תל אביב, ישראל"), cities_of("תל אביב, ישראל")) == (["IL"], ["Tel Aviv"]),
      repr((countries_of("תל אביב, ישראל"), cities_of("תל אביב, ישראל"))))
check("and the Hebrew and English spellings agree on the name",
      cities_of("הרצליה") == cities_of("Herzliya"),
      repr((cities_of("הרצליה"), cities_of("Herzliya"))))


# Work arrangements are not places.
# These sit exactly where a city would and would otherwise become the
# most popular city on the board.
check("Remote is neither a city nor a country",
      cities_of("Remote") == [] and countries_of("Remote") == [],
      repr((cities_of("Remote"), countries_of("Remote"))))
check("Distributed, Global is neither",
      cities_of("Distributed, Global") == [] and countries_of("Distributed, Global") == [],
      repr((cities_of("Distributed, Global"), countries_of("Distributed, Global"))))
check("Hybrid is not a city", cities_of("Hybrid") == [], repr(cities_of("Hybrid")))

for loc in ["Onsite", "On-site", "Anywhere", "Worldwide", "Global", "Flexible",
            "Various", "Multiple Locations", "Remote, EMEA", "Remote, Global",
            "Remote - APAC", "Europe"]:
    check("%r is not a city" % loc, cities_of(loc) == [], repr(cities_of(loc)))

# The arrangement can still carry a real place beside it.
check("Hybrid - Tel Aviv keeps the city",
      cities_of("Hybrid - Tel Aviv") == ["Tel Aviv"], repr(cities_of("Hybrid - Tel Aviv")))
countries_are("Remote (Israel)", ["IL"])


# Nothing at all, which arrives from every board sooner or later.
for empty in [None, "", "   ", "\n", ",", " , ; ", "-"]:
    got = (countries_of(empty), cities_of(empty), country_string(empty), city_string(empty))
    check("%r is empty rather than an error" % empty, got == ([], [], "", ""), repr(got))


# Labels, which are what the filter shows instead of a code.
unlabelled = sorted(code for code in ALPHA2 if not label_for(code) or label_for(code) == code)
check("every code the resolver can return has a display name", not unlabelled,
      repr(unlabelled))
check("every label is in the table", all(code in COUNTRY_LABELS for code in ALPHA2),
      repr(sorted(set(ALPHA2) - set(COUNTRY_LABELS))))
for code, name in [("US", "United States"), ("GB", "United Kingdom"), ("IL", "Israel"),
                   ("CZ", "Czechia"), ("KR", "South Korea"),
                   ("AE", "United Arab Emirates"), ("NL", "Netherlands"),
                   ("CO", "Colombia"), ("ME", "Montenegro"), ("DO", "Dominican Republic")]:
    check("%s shows as %s" % (code, name), label_for(code) == name, repr(label_for(code)))

# An unknown code shows as itself rather than vanishing from the filter,
# which is how a code added to the resolver before the label table would
# otherwise disappear.
check("an unknown code falls back to itself", label_for("ZZ") == "ZZ", repr(label_for("ZZ")))
check("and so does an empty one", label_for("") == "", repr(label_for("")))


# The stored form. Same shape as skills: comma-joined, no spaces, so the
# SQL that filters on it can wrap the value in commas and match exactly.
for loc in ["Remote, Canada; Remote, Israel; Remote, United Kingdom",
            "Tel Aviv, Israel", "Chicago, IL", "Remote", "", None,
            "Boston, MA; Chicago, IL; Denver, CO; Austin, TX"]:
    check("country_string(%r) is the joined list" % loc,
          country_string(loc) == ",".join(countries_of(loc)), repr(country_string(loc)))
    check("city_string(%r) is the joined list" % loc,
          city_string(loc) == ",".join(cities_of(loc)), repr(city_string(loc)))

check("the joined countries carry no spaces",
      country_string("Remote, Canada; Remote, Israel; Remote, United Kingdom") == "CA,IL,GB",
      repr(country_string("Remote, Canada; Remote, Israel; Remote, United Kingdom")))
check("and neither do the joined cities",
      city_string("Tel Aviv, Israel; Herzliya; Ramat Gan") == "Tel Aviv,Herzliya,Ramat Gan",
      repr(city_string("Tel Aviv, Israel; Herzliya; Ramat Gan")))


# Table invariants. These are cheap and they catch the typo in the pull
# request that adds a city, which is the only way this data ever changes.
bad_city_codes = sorted({v for v in CITIES.values()} - ALPHA2)
check("every city points at a country the resolver knows", not bad_city_codes,
      repr(bad_city_codes))
bad_names = sorted({v for v in countries.COUNTRY_NAMES.values()
                    if len(v) != 2 or not v.isupper()})
check("every country code is two uppercase letters", not bad_names, repr(bad_names))
bad_alpha3 = sorted(k for k in countries.COUNTRY_ALPHA3 if len(k) != 3 or not k.isupper())
check("every alpha-3 key is three uppercase letters", not bad_alpha3, repr(bad_alpha3))
bad_alias_targets = sorted(k for k, v in CITY_ALIASES.items() if not v or v != v.strip())
check("no alias points at a blank name", not bad_alias_targets, repr(bad_alias_targets))

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
