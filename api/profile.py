"""The signed-in user's own profile: what they can do, not who they are.

Deliberately holds no identity. A profile is a skills list, a seniority
and a couple of search preferences, which is everything the matching
needs and nothing a breach would make anyone's day worse for leaking.
No name, no phone, no employment history, and no CV file: uploads are
parsed for skills and the document is thrown away.

Lives in the alerts table under a sentinel sort key rather than a table
of its own. One row per user, same partition as their alerts, so a
delete-my-account is one Query and one BatchWrite. The evaluator scans
with a filter on `active`, which a profile row does not carry, so it is
invisible there (see alerts.py).

Values are validated against closed vocabularies on the way in. That is
the reason this file exists rather than storing whatever JSON arrives:
the only strings that reach the table are ones this project already
uses, so a profile cannot become a place to park arbitrary user text.
"""

PROFILE_ID = "#profile"

# One definition, in skills.py, compiled into regexes by probe.py and
# used as a closed list here. These were briefly two hand-maintained
# lists and disagreed about fourteen entries within minutes, which would
# have meant profile skills that could never match a job.
from skills import SKILL_LABELS as SKILLS  # noqa: E402

SENIORITY = [
    "intern", "junior", "mid", "senior", "staff",
    "principal", "lead", "manager", "director", "exec",
]

WORKPLACE = ["remote", "hybrid", "onsite"]

# A profile is a search, not a CV. Past this many skills it stops
# narrowing anything and starts being a list of everything the person
# has ever touched.
MAX_SKILLS = 20


def empty_profile() -> dict:
    return {"skills": [], "seniority": None, "workplace": [], "israel_only": True}


def clean_profile(body: dict) -> dict:
    """Whatever arrived, reduced to values this project recognises.

    Unknown values are dropped rather than rejected: a profile is a set
    of preferences, and refusing the whole save because one skill is
    misspelled would lose the rest of someone's edit for no benefit.
    """
    if not isinstance(body, dict):
        raise ValueError("profile must be an object")

    known_skills = {s.lower(): s for s in SKILLS}
    skills, seen = [], set()
    for raw in body.get("skills") or []:
        canonical = known_skills.get(str(raw).strip().lower())
        if canonical and canonical not in seen:
            seen.add(canonical)
            skills.append(canonical)
        if len(skills) >= MAX_SKILLS:
            break

    seniority = str(body.get("seniority") or "").strip().lower()
    workplace = [w for w in (
        str(x).strip().lower() for x in (body.get("workplace") or [])
    ) if w in WORKPLACE]

    return {
        "skills": skills,
        "seniority": seniority if seniority in SENIORITY else None,
        # dict.fromkeys, not set: the order someone picked them in is
        # the order they see them back.
        "workplace": list(dict.fromkeys(workplace)),
        "israel_only": bool(body.get("israel_only", True)),
    }


def profile_to_filter(profile: dict) -> dict:
    """A saved profile as the query params /api/jobs already accepts.

    The whole point of validating against closed vocabularies above: a
    profile is directly expressible as a search, so matching needs no
    second engine and no second notion of what a skill is.
    """
    params = {}
    if profile.get("skills"):
        # The dedicated OR-and-rank param, not q and not keywords.
        # Reported live, from a CV that found twelve skills: q is a
        # single substring match against title and company, so it looked
        # for the literal phrase "Python Azure Linux CI/CD Git
        # Terraform..." and found nothing, forever. keywords would have
        # been worse in a quieter way: it is AND-matched, so it would
        # have demanded one job requiring all twelve.
        params["skills"] = ",".join(profile["skills"])
    if profile.get("seniority"):
        params["seniority"] = profile["seniority"]
    if profile.get("workplace"):
        params["workplace"] = ",".join(profile["workplace"])
    if profile.get("israel_only"):
        params["israel_only"] = "1"
    return params
