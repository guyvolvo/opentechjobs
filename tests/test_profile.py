"""The account profile: skills, seniority and preferences, no identity.

The test that matters most here is the vocabulary one. The skill list
started as two hand-maintained copies, one in probe.py for tagging jobs
and one in the API for validating a profile, and they disagreed about
fourteen entries within minutes of being written. A profile skill the
tagger never emits can never match a job, and nothing would have failed
loudly. They share one definition now, and this fails if that stops
being true.

Run directly, no framework:  python tests/test_profile.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

os.environ.setdefault("ALERTS_TABLE", "test-alerts")
os.environ.setdefault("DATA_BUCKET", "test-bucket")
os.environ.setdefault("DATA_KEY", "jobs.db")
os.environ.setdefault("AWS_DEFAULT_REGION", "il-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import probe  # noqa: E402
import profile  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s: %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))
    if not ok:
        failures.append(name)


# The whole point of the shared module.
tagger = [label for label, _ in probe._SKILL_KEYWORDS]
check("the profile offers exactly the skills the tagger emits",
      set(profile.SKILLS) == set(tagger),
      f"only in profile: {sorted(set(profile.SKILLS) - set(tagger))}; "
      f"only in probe: {sorted(set(tagger) - set(profile.SKILLS))}")
check("and in the same order, so both read the same list",
      profile.SKILLS == tagger)

# Every skill a user can pick has to be one a job can carry, which is
# what makes matching a plain search rather than a mapping problem.
extracted = probe._extract_skills("Engineer", " ".join(profile.SKILLS))
check("skills round-trip through the tagger",
      all(s in profile.SKILLS for s in extracted), repr(extracted))

# Validation drops rather than rejects.
p = profile.clean_profile({
    "skills": ["python", "KUBERNETES", "not-a-real-skill", "Go", "go"],
    "seniority": "Senior",
    "workplace": ["remote", "moon"],
    "israel_only": True,
})
check("known skills are canonicalised", p["skills"] == ["Python", "Kubernetes", "Go"], repr(p["skills"]))
check("an unknown skill is dropped, not fatal", "not-a-real-skill" not in p["skills"])
check("a repeat is dropped", p["skills"].count("Go") == 1)
check("seniority is lowercased and checked", p["seniority"] == "senior")
check("an unknown workplace is dropped", p["workplace"] == ["remote"], repr(p["workplace"]))

check("an unknown seniority becomes none",
      profile.clean_profile({"seniority": "overlord"})["seniority"] is None)
check("israel_only defaults on", profile.clean_profile({})["israel_only"] is True)
check("and can be turned off",
      profile.clean_profile({"israel_only": False})["israel_only"] is False)

many = profile.clean_profile({"skills": profile.SKILLS * 2})
check("skills are capped", len(many["skills"]) == profile.MAX_SKILLS, str(len(many["skills"])))

def rejects(value):
    try:
        profile.clean_profile(value)
    except ValueError:
        return True
    return False


check("a list is rejected outright", rejects(["python"]))
check("so is a string", rejects("python"))

# Identity must never survive a save: nothing outside the known keys is
# stored, so a client sending a CV's contact details cannot persist them.
saved = profile.clean_profile({
    "skills": ["Python"], "name": "A Person", "email": "a@example.com",
    "phone": "+972000000", "cv_text": "twenty years of history",
})
check("nothing outside the known keys is stored",
      set(saved) == {"skills", "seniority", "workplace", "israel_only"}, repr(sorted(saved)))

# A profile is expressible as an ordinary board search.
f = profile.profile_to_filter({"skills": ["Python", "Go"], "seniority": "senior",
                               "workplace": ["remote"], "israel_only": True})
check("skills become the OR-and-rank param, not q or keywords",
      f.get("skills") == "Python,Go" and "q" not in f and "keywords" not in f, repr(f))
check("seniority and workplace map straight across",
      f.get("seniority") == "senior" and f.get("workplace") == "remote", repr(f))
check("israel_only maps to the board's own param", f.get("israel_only") == "1")
# Not an empty search: a profile nobody has filled in still means "show
# me Israeli listings", which is the default this board exists for.
check("an unfilled profile is still an Israel-only search",
      profile.profile_to_filter(profile.empty_profile()) == {"israel_only": "1"},
      repr(profile.profile_to_filter(profile.empty_profile())))
check("and turning that off leaves nothing to filter on",
      profile.profile_to_filter({**profile.empty_profile(), "israel_only": False}) == {})

# The row hides in the alerts partition, so it must not look like one.
check("the profile sort key cannot collide with a uuid4 alert id",
      profile.PROFILE_ID.startswith("#"), profile.PROFILE_ID)

import handler  # noqa: E402


class FakeTable:
    """Returns one profile row and one real alert from the same Query."""

    def query(self, **kw):
        return {"Items": [
            {"user_id": "u", "alert_id": profile.PROFILE_ID, "skills": ["Python"]},
            {"user_id": "u", "alert_id": "abc-123", "filter": {"q": "devops"}, "active": True},
        ]}

    def get_item(self, Key=None):
        return {"Item": {"skills": ["Python"], "seniority": "senior"}}


handler._alerts_table = FakeTable()

listed = handler.route_list_alerts("u")["alerts"]
check("the profile row is not returned as an alert",
      [a["alert_id"] for a in listed] == ["abc-123"], repr(listed))

got = handler.route_get_profile("u")
check("the profile route returns the stored values",
      got["profile"]["skills"] == ["Python"] and got["profile"]["seniority"] == "senior",
      repr(got.get("profile")))
check("and ships the vocabularies the page needs",
      got["options"]["skills"] == profile.SKILLS
      and got["options"]["workplace"] == profile.WORKPLACE)


class EmptyTable(FakeTable):
    def get_item(self, Key=None):
        return {}


handler._alerts_table = EmptyTable()
check("a user who never saved one gets an empty profile, not a 404",
      handler.route_get_profile("u")["profile"] == profile.empty_profile())

print()
if failures:
    print("%d failed:" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all passed")
