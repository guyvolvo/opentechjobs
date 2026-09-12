"""One country per location string, from whatever the ATS happened to write.

There is no country field to read. Every board writes location as free
text and no two agree, so this file is the place where "München, de",
"Remote, United Kingdom", "Tel Aviv-Yafo, Tel Aviv, ISR" and "Prague"
all become a country code.

Shared by probe.py, which tags jobs as they are scraped, and the API,
which filters on the result. One definition, for the same reason
skills.py exists: two copies of a vocabulary disagree within minutes.

The ordering of the rules below is the whole design, and it comes from
counting 1,500 real locations rather than from taste:

  lowercase two-letter   us 318, ca 52, pl 25, fr 19  -- always a country
  UPPERCASE two-letter   CA 73, NY 35, DC 20, IL 4    -- almost always a US state
  UPPERCASE three-letter USA 20, CUN 7, NYC 4         -- countries AND airport codes

That second row is the trap, and it is pointed straight at this board.
Every bare uppercase IL in the data is Chicago, Illinois; not one is
Israel. A resolver that reads two-letter codes as countries turns every
Chicago listing into an Israeli one, on a site whose entire premise is
Israeli listings. So a US state code is the last rule tried, and only
after the city has had its say: "Chicago, IL" resolves through Chicago,
and "Tel Aviv, IL" through Tel Aviv, and neither needs the state rule at
all.

Airport codes are why alpha-3 is an allowlist rather than a computed
set. CUN and SEA and NYC all look exactly like country codes.
"""

import re
import unicodedata

# Rule 1. Names and the aliases people actually write. Lowercased keys.
COUNTRY_NAMES = {
    "israel": "IL", "state of israel": "IL", "ישראל": "IL", "מדינת ישראל": "IL",
    "united states": "US", "united states of america": "US", "usa": "US",
    "u.s.": "US", "u.s.a.": "US", "america": "US",
    "united kingdom": "GB", "great britain": "GB", "britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "germany": "DE", "deutschland": "DE",
    "france": "FR", "spain": "ES", "portugal": "PT", "italy": "IT",
    "netherlands": "NL", "the netherlands": "NL", "holland": "NL",
    "belgium": "BE", "luxembourg": "LU", "ireland": "IE", "republic of ireland": "IE",
    "switzerland": "CH", "austria": "AT", "denmark": "DK", "sweden": "SE",
    "norway": "NO", "finland": "FI", "iceland": "IS", "estonia": "EE",
    "latvia": "LV", "lithuania": "LT", "poland": "PL",
    "czechia": "CZ", "czech republic": "CZ", "slovakia": "SK", "hungary": "HU",
    "romania": "RO", "bulgaria": "BG", "greece": "GR", "cyprus": "CY",
    "croatia": "HR", "slovenia": "SI", "serbia": "RS", "ukraine": "UA",
    "turkey": "TR", "türkiye": "TR", "russia": "RU",
    "canada": "CA", "mexico": "MX", "brazil": "BR", "brasil": "BR",
    "argentina": "AR", "chile": "CL", "colombia": "CO", "peru": "PE",
    "uruguay": "UY", "costa rica": "CR", "panama": "PA", "dominican republic": "DO",
    "india": "IN", "china": "CN", "hong kong": "HK", "taiwan": "TW",
    "japan": "JP", "south korea": "KR", "korea": "KR", "republic of korea": "KR",
    "singapore": "SG", "malaysia": "MY", "thailand": "TH", "vietnam": "VN",
    "viet nam": "VN", "indonesia": "ID", "philippines": "PH", "pakistan": "PK",
    "bangladesh": "BD", "sri lanka": "LK",
    "australia": "AU", "new zealand": "NZ",
    "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA",
    "qatar": "QA", "bahrain": "BH", "kuwait": "KW", "jordan": "JO",
    "egypt": "EG", "morocco": "MA", "tunisia": "TN", "south africa": "ZA",
    "kenya": "KE", "nigeria": "NG", "ghana": "GH", "ethiopia": "ET",
    "mauritius": "MU", "armenia": "AM",
    "maldives": "MV", "tanzania": "TZ", "kazakhstan": "KZ", "uzbekistan": "UZ",
    "azerbaijan": "AZ", "belarus": "BY", "moldova": "MD", "albania": "AL",
    "north macedonia": "MK", "bosnia and herzegovina": "BA", "montenegro": "ME",
    "malta": "MT", "monaco": "MC", "andorra": "AD", "liechtenstein": "LI",
    "guatemala": "GT", "honduras": "HN", "el salvador": "SV", "nicaragua": "NI",
    "bolivia": "BO", "paraguay": "PY", "ecuador": "EC", "venezuela": "VE",
    "jamaica": "JM", "trinidad and tobago": "TT", "barbados": "BB",
    "senegal": "SN", "rwanda": "RW", "uganda": "UG", "zambia": "ZM",
    "algeria": "DZ", "oman": "OM", "lebanon": "LB", "nepal": "NP",
    "myanmar": "MM", "cambodia": "KH", "mongolia": "MN", "fiji": "FJ",
}

# Rule 2. Alpha-3, allowlisted rather than computed, because CUN, NYC and
# SEA are airport codes that look identical to one.
COUNTRY_ALPHA3 = {
    "ISR": "IL", "USA": "US", "GBR": "GB", "DEU": "DE", "FRA": "FR",
    "ESP": "ES", "PRT": "PT", "ITA": "IT", "NLD": "NL", "BEL": "BE",
    "LUX": "LU", "IRL": "IE", "CHE": "CH", "AUT": "AT", "DNK": "DK",
    "SWE": "SE", "NOR": "NO", "FIN": "FI", "ISL": "IS", "EST": "EE",
    "LVA": "LV", "LTU": "LT", "POL": "PL", "CZE": "CZ", "SVK": "SK",
    "HUN": "HU", "ROU": "RO", "BGR": "BG", "GRC": "GR", "CYP": "CY",
    "HRV": "HR", "SVN": "SI", "SRB": "RS", "UKR": "UA", "TUR": "TR",
    "CAN": "CA", "MEX": "MX", "BRA": "BR", "ARG": "AR", "CHL": "CL",
    "COL": "CO", "PER": "PE", "URY": "UY", "CRI": "CR", "PAN": "PA",
    "IND": "IN", "CHN": "CN", "HKG": "HK", "TWN": "TW", "JPN": "JP",
    "KOR": "KR", "SGP": "SG", "MYS": "MY", "THA": "TH", "VNM": "VN",
    "IDN": "ID", "PHL": "PH", "PAK": "PK", "BGD": "BD", "LKA": "LK",
    "AUS": "AU", "NZL": "NZ", "ARE": "AE", "SAU": "SA", "QAT": "QA",
    "EGY": "EG", "MAR": "MA", "ZAF": "ZA", "KEN": "KE", "NGA": "NG",
    "GEO": "GE", "ARM": "AM", "AZE": "AZ", "KAZ": "KZ", "MDV": "MV",
}

# Rule 3. Lowercase alpha-2, which is one ATS's own convention and is
# reliable precisely because it is lowercase. Only the codes above are
# accepted, so a stray "hi" or "ok" is not a country.
ALPHA2 = set(COUNTRY_NAMES.values()) | set(COUNTRY_ALPHA3.values())

# Rule 4. Cities that appear with no country beside them. Israel's list
# is the one that has to be complete, since it decides this board's
# headline filter; the rest are the cities the data actually shows.
CITIES = {
    # Israel
    "tel aviv": "IL", "tel aviv-yafo": "IL", "tel-aviv": "IL", "telaviv": "IL",
    "jerusalem": "IL", "haifa": "IL", "herzliya": "IL", "herzliya pituach": "IL",
    "raanana": "IL", "ra'anana": "IL", "petah tikva": "IL", "petach tikva": "IL",
    "netanya": "IL", "rehovot": "IL", "ramat gan": "IL", "givatayim": "IL",
    "beer sheva": "IL", "be'er sheva": "IL", "beersheba": "IL", "yokneam": "IL",
    "kiryat gat": "IL", "caesarea": "IL", "modiin": "IL", "ashdod": "IL",
    "holon": "IL", "bnei brak": "IL", "airport city": "IL", "rosh haayin": "IL",
    "or yehuda": "IL", "kfar saba": "IL", "hod hasharon": "IL", "migdal haemek": "IL",
    # Hebrew. Niloosoft and the Israeli boards write locations in it, and
    # an unmatched Hebrew string used to mean no city and no country at
    # all, on exactly the listings this board exists for. The regional
    # ones are regions rather than cities, kept as themselves rather than
    # forced into a nearby city they are not.
    "תל אביב": "IL", "תל־אביב": "IL",
    "ירושלים": "IL", "חיפה": "IL",
    "הרצליה": "IL", "רעננה": "IL",
    "פתח תקווה": "IL", "נתניה": "IL",
    "רחובות": "IL", "רמת גן": "IL",
    "באר שבע": "IL", "אשדוד": "IL",
    "חולון": "IL", "כפר סבא": "IL",
    "אזור המרכז": "IL", "אזור השפלה": "IL",
    "אזור השרון": "IL", "אזור הצפון": "IL",
    "אזור הדרום": "IL",
    "ירושלים והסביבה": "IL",
    # United States
    "new york": "US", "new york city": "US", "nyc": "US", "brooklyn": "US",
    "san francisco": "US", "palo alto": "US", "mountain view": "US",
    "sunnyvale": "US", "santa clara": "US", "san jose": "US", "cupertino": "US",
    "seattle": "US", "bellevue": "US", "redmond": "US", "austin": "US",
    "chicago": "US", "denver": "US", "boston": "US", "atlanta": "US",
    "dallas": "US", "houston": "US", "miami": "US", "los angeles": "US",
    "san diego": "US", "philadelphia": "US", "phoenix": "US", "portland": "US",
    "washington": "US", "washington dc": "US", "pittsburgh": "US",
    "minneapolis": "US", "detroit": "US", "nashville": "US", "raleigh": "US",
    # Rest of the world
    "london": "GB", "cambridge, uk": "GB", "manchester": "GB", "edinburgh": "GB",
    "dublin": "IE", "paris": "FR", "toulouse": "FR", "lyon": "FR",
    "berlin": "DE", "munich": "DE", "münchen": "DE", "hamburg": "DE",
    "frankfurt": "DE", "cologne": "DE", "köln": "DE", "stuttgart": "DE",
    "amsterdam": "NL", "rotterdam": "NL", "eindhoven": "NL",
    "brussels": "BE", "zurich": "CH", "zürich": "CH", "geneva": "CH",
    "vienna": "AT", "prague": "CZ", "praha": "CZ", "brno": "CZ",
    "warsaw": "PL", "warszawa": "PL", "krakow": "PL", "kraków": "PL",
    "wroclaw": "PL", "gdansk": "PL", "budapest": "HU", "bucharest": "RO",
    "sofia": "BG", "belgrade": "RS", "zagreb": "HR", "ljubljana": "SI",
    "athens": "GR", "lisbon": "PT", "porto": "PT", "madrid": "ES",
    "barcelona": "ES", "valencia": "ES", "milan": "IT", "rome": "IT",
    "stockholm": "SE", "gothenburg": "SE", "copenhagen": "DK", "oslo": "NO",
    "helsinki": "FI", "tallinn": "EE", "riga": "LV", "vilnius": "LT",
    "kyiv": "UA", "kiev": "UA", "istanbul": "TR", "tbilisi": "GE",
    "toronto": "CA", "vancouver": "CA", "montreal": "CA", "ottawa": "CA",
    "waterloo": "CA", "calgary": "CA",
    "mexico city": "MX", "ciudad de méxico": "MX", "ciudad de mexico": "MX",
    "cdmx": "MX", "guadalajara": "MX", "sao paulo": "BR",
    "são paulo": "BR", "rio de janeiro": "BR", "buenos aires": "AR",
    "santiago": "CL", "bogota": "CO", "bogotá": "CO", "lima": "PE",
    "bangalore": "IN", "bengaluru": "IN", "mumbai": "IN", "pune": "IN",
    "hyderabad": "IN", "chennai": "IN", "gurgaon": "IN", "gurugram": "IN",
    "noida": "IN", "new delhi": "IN", "delhi": "IN",
    "tokyo": "JP", "osaka": "JP", "seoul": "KR", "taipei": "TW",
    "shanghai": "CN", "beijing": "CN", "shenzhen": "CN", "hong kong": "HK",
    "singapore": "SG", "kuala lumpur": "MY", "bangkok": "TH",
    "ho chi minh city": "VN", "hanoi": "VN", "jakarta": "ID", "manila": "PH",
    "sydney": "AU", "melbourne": "AU", "brisbane": "AU", "auckland": "NZ",
    "dubai": "AE", "abu dhabi": "AE", "riyadh": "SA", "doha": "QA",
    "cairo": "EG", "casablanca": "MA", "nairobi": "KE", "lagos": "NG",
    "cape town": "ZA", "johannesburg": "ZA",
}

# Rule 5, last and only if nothing above fired. Half of these collide
# with a country code, which is exactly why they are last: IL is
# Illinois far more often than it is Israel, in this data always.
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR",
}

# Spelled out, because "Lehi, Utah" and "Berthoud, Colorado" are how
# some boards write a US address and neither carries a country anywhere.
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york state",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington state",
    "west virginia", "wisconsin", "wyoming", "puerto rico",
}

# Written by people, not machines, so a country can be spelled out mid
# sentence: "Remote - France", "Remote (Israel)".
_SPLIT = re.compile(r"[;|/,\n()\[\]]+|\s+-\s+")
_ALPHA2_RE = re.compile(r"^[a-z]{2}$")
_ALPHA3_RE = re.compile(r"^[A-Z]{3}$")
_UPPER2_RE = re.compile(r"^[A-Z]{2}$")


def _norm(text: str) -> str:
    """Fold everything that makes one place look like two.

    "Ramat-Gan" and "Ramat Gan" were two separate entries in the filter
    for one city, and so were Montreal and Montreal with its accent.
    Accents come off, hyphens and apostrophes go, runs of space collapse.
    """
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    for ch in ("-", "_", "־", "‐", "‑", "–", "—"):
        folded = folded.replace(ch, " ")
    folded = folded.replace("'", "").replace("’", "")
    return " ".join(folded.lower().split())

# Longest first, so "united states" wins over "united" and "new york
# state" over "new york". Word-bounded, or "india" matches "indiana" and
# "chile" matches nothing good either.
def _fold_keys(mapping: dict) -> dict:
    return {_norm(k): v for k, v in mapping.items()}


CITIES_FOLDED = _fold_keys(CITIES)
COUNTRY_NAMES_FOLDED = _fold_keys(COUNTRY_NAMES)
US_STATE_NAMES_FOLDED = {_norm(x) for x in US_STATE_NAMES}

_PHRASE_RE = re.compile(
    r"\b(?:" + "|".join(
        re.escape(n) for n in sorted(
            set(COUNTRY_NAMES_FOLDED) | US_STATE_NAMES_FOLDED | {"us", "uk", "usa"},
            key=len, reverse=True)
    ) + r")\b")

# What to call a code on screen. Built from the names above rather than
# typed twice: the first spelling listed for each code is the canonical
# one, which is why "united states" is written before "usa" and
# "czechia" before "czech republic".
def _label(name: str) -> str:
    small = {"and", "of", "the"}
    return " ".join(w if w in small else w.capitalize() for w in name.split())


COUNTRY_LABELS: dict[str, str] = {}
for _name, _code in COUNTRY_NAMES.items():
    COUNTRY_LABELS.setdefault(_code, _label(_name))
COUNTRY_LABELS["US"] = "United States"
COUNTRY_LABELS["GB"] = "United Kingdom"
COUNTRY_LABELS["AE"] = "United Arab Emirates"
COUNTRY_LABELS["KR"] = "South Korea"
COUNTRY_LABELS["CZ"] = "Czechia"
COUNTRY_LABELS["GE"] = "Georgia"


def label_for(code: str) -> str:
    """A country's display name, falling back to the code itself so an
    unlabelled one shows as "MV" rather than vanishing from a filter."""
    return COUNTRY_LABELS.get(code, code)


MAX_COUNTRIES = 8


def _segment_country(seg: str) -> str | None:
    raw = seg.strip().strip(".")
    if not raw:
        return None
    low = _norm(raw)

    if low in COUNTRY_NAMES_FOLDED:
        return COUNTRY_NAMES_FOLDED[low]
    if _ALPHA3_RE.match(raw) and raw in COUNTRY_ALPHA3:
        return COUNTRY_ALPHA3[raw]
    if _ALPHA2_RE.match(raw) and raw.upper() in ALPHA2:
        return raw.upper()
    if low in CITIES_FOLDED:
        return CITIES_FOLDED[low]
    # "Remote, United Kingdom" already matched above; this catches a city
    # written with its own qualifier, like "Herzliya Pituach Office".
    for city, code in CITIES_FOLDED.items():
        if low.startswith(city + " ") or low.endswith(" " + city):
            return code
    if raw == "UK":
        return "GB"
    if raw == "US":
        return "US"
    if low in US_STATE_NAMES_FOLDED:
        return "US"
    # Last within the segment: a country or state named inside a phrase.
    # "US Remote", "Ukraine Office", "All France (remote)" are all one
    # segment with the answer sitting in the middle of it.
    m = _PHRASE_RE.search(low)
    if m:
        name = m.group(0)
        if name in ("us", "u.s.", "usa"):
            return "US"
        if name == "uk":
            return "GB"
        return COUNTRY_NAMES_FOLDED.get(name) or ("US" if name in US_STATE_NAMES_FOLDED else None)
    return None


def countries_of(location: str | None) -> list[str]:
    """Every country a location string names, in the order written.

    Deduped: "Remote, Israel; Tel Aviv, Israel" is one country, not two,
    and a job listing four offices in the same country is one entry in
    the filter rather than four.
    """
    if not location:
        return []
    out: list[str] = []
    # Semicolons separate whole locations; everything else separates the
    # parts of one. Both are split the same way here because a country
    # found anywhere in a group belongs to the job either way.
    groups = [g for g in location.split(";") if g.strip()] or [location]
    for group in groups:
        segments = [s for s in _SPLIT.split(group) if s.strip()]
        found = None
        state_fallback = None
        for seg in segments:
            code = _segment_country(seg)
            if code:
                found = code
                break
            if state_fallback is None and _UPPER2_RE.match(seg.strip()) and seg.strip() in US_STATES:
                state_fallback = "US"
        code = found or state_fallback
        if code and code not in out:
            out.append(code)
    return out[:MAX_COUNTRIES]


def country_string(location: str | None) -> str:
    """The stored form: comma-joined, no spaces, same shape as skills."""
    return ",".join(countries_of(location))


# The city half of the same problem, and the reason the filter can be one
# dropdown instead of two.
#
# Raw location strings spell one city many ways: "Tel Aviv", "Tel
# Aviv-Yafo, Tel Aviv, ISR", "tel-aviv", "Tel Aviv, Israel". Faceting on
# the raw string offers all four as separate options for the same place,
# which is the duplication this exists to remove. A gazetteer hit
# collapses them to one name; anything else keeps its own first segment,
# so a city we have never heard of still appears rather than vanishing.
CITY_ALIASES = {
    "tel aviv-yafo": "Tel Aviv", "tel-aviv": "Tel Aviv", "telaviv": "Tel Aviv",
    "ra'anana": "Raanana", "petach tikva": "Petah Tikva",
    "be'er sheva": "Beer Sheva", "beersheba": "Beer Sheva",
    "herzliya pituach": "Herzliya", "rosh haayin": "Rosh HaAyin",
    "new york city": "New York", "nyc": "New York",
    "washington dc": "Washington",
    "münchen": "Munich", "köln": "Cologne", "zürich": "Zurich",
    "praha": "Prague", "warszawa": "Warsaw", "kraków": "Krakow",
    "kiev": "Kyiv", "são paulo": "Sao Paulo", "bogotá": "Bogota",
    "bengaluru": "Bangalore", "gurugram": "Gurgaon",
    "ciudad de méxico": "Mexico City", "ciudad de mexico": "Mexico City",
    "cdmx": "Mexico City",
    # Hebrew to the same English name the rest of the board uses, so one
    # city is one entry however the board that posted it writes.
    "תל אביב": "Tel Aviv", "ירושלים": "Jerusalem",
    "חיפה": "Haifa", "הרצליה": "Herzliya",
    "רעננה": "Raanana", "פתח תקווה": "Petah Tikva",
    "נתניה": "Netanya", "רחובות": "Rehovot",
    "רמת גן": "Ramat Gan", "באר שבע": "Beer Sheva",
    "אשדוד": "Ashdod", "חולון": "Holon",
    "כפר סבא": "Kfar Saba",
    "אזור המרכז": "Central District",
    "אזור השפלה": "Shfela",
    "אזור השרון": "Sharon",
    "אזור הצפון": "Northern District",
    "אזור הדרום": "Southern District",
    "ירושלים והסביבה": "Jerusalem",
}

# A work arrangement, not a place. These sit where a city would and would
# otherwise become the most popular "city" on the board.
_NOT_A_CITY = {
    "remote", "hybrid", "onsite", "on-site", "office", "global", "worldwide",
    "distributed", "anywhere", "flexible", "various", "multiple locations",
    "location", "all", "emea", "apac", "amer", "latam", "europe", "asia",
    "north america", "south america", "latin america", "middle east", "africa",
}

CITY_ALIASES_FOLDED = _fold_keys(CITY_ALIASES)
_NOT_A_CITY_FOLDED = {_norm(x) for x in _NOT_A_CITY}

MAX_CITIES = 4


def _canonical_city(seg: str) -> str | None:
    low = _norm(seg.strip().strip("."))
    if not low or low in _NOT_A_CITY_FOLDED:
        return None
    if low in CITY_ALIASES_FOLDED:
        return CITY_ALIASES_FOLDED[low]
    if low in CITIES_FOLDED:
        return low.title()
    # A qualifier tacked onto a known city: "Herzliya Pituach Office",
    # "Chicago, IL (Southwest)". Longest first so "new york city" is not
    # shadowed by "new york".
    for city in sorted(CITIES_FOLDED, key=len, reverse=True):
        if low.startswith(city + " ") or low.endswith(" " + city):
            return CITY_ALIASES_FOLDED.get(city, city.title())
    # Not a place we know, but not obviously not one either. Anything
    # that is really a country, a state or a code has already been
    # excluded by the caller.
    if len(low) > 2 and not _ALPHA3_RE.match(seg.strip()) and not _UPPER2_RE.match(seg.strip()):
        return seg.strip()
    return None


def cities_of(location: str | None) -> list[str]:
    """Every city a location names, canonicalised and deduplicated."""
    if not location:
        return []
    out: list[str] = []
    groups = [g for g in location.split(";") if g.strip()] or [location]
    for group in groups:
        for seg in _SPLIT.split(group):
            seg = seg.strip()
            if not seg:
                continue
            # A segment that resolves to a country is a country, not a
            # city, however much "Georgia" might want to be both.
            if _segment_country(seg) and _norm(seg) not in CITIES_FOLDED:
                continue
            city = _canonical_city(seg)
            if city and city not in out:
                out.append(city)
                break  # one city per location, the first one written
    return out[:MAX_CITIES]


def city_string(location: str | None) -> str:
    return ",".join(cities_of(location))
