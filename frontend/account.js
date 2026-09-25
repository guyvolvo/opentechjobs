// The account page. Loads after app.js and reuses its auth helpers,
// alert form and escapeHtml rather than a second copy of each; app.js
// stops its own boot early when the board markup is absent.
//
// Everything here needs a token, so the page is one of two states: a
// prompt to sign in, or the real thing. There is no useful half-signed-in
// version.

const $ = (id) => document.getElementById(id);

// Served by GET /me/profile alongside the profile itself: the same
// terms probe.py tags jobs with, so a skill found in a CV is by
// construction one a listing can carry. A hardcoded copy here drifted
// from the real list within minutes the first time it was tried.
const draft = { skills: [], seniority: "", workplace: [], israel_only: true, country: [], city: [], cadence: "instant",
                digest_time: "09:00", digest_tz: "Asia/Jerusalem", digest_day: 0 };

function setStatus(el, text, isError = false) {
  const node = $(el);
  if (!node) return;
  node.textContent = text;
  node.classList.toggle("error", !!isError);
  if (text) setTimeout(() => { node.textContent = ""; node.classList.remove("error"); }, 4000);
}

const EMPTY_PROFILE = { skills: [], seniority: null, workplace: [], israel_only: true, country: [], city: [], cadence: "instant",
                        digest_time: "09:00", digest_tz: "Asia/Jerusalem", digest_day: 0 };

const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

// The zones on offer. Not the whole IANA list, which is six hundred
// names nobody scrolls; the browser's own zone is always added, so
// whoever is somewhere else still finds home at the top.
const ZONES = [
  "Asia/Jerusalem", "UTC", "Europe/London", "Europe/Dublin", "Europe/Lisbon", "Europe/Paris", "Europe/Berlin",
  "Europe/Amsterdam", "Europe/Madrid", "Europe/Rome", "Europe/Warsaw", "Europe/Athens", "Europe/Kyiv",
  "Europe/Istanbul", "Europe/Moscow", "America/New_York", "America/Toronto", "America/Chicago", "America/Denver",
  "America/Los_Angeles", "America/Vancouver", "America/Sao_Paulo", "America/Mexico_City", "Asia/Dubai",
  "Asia/Kolkata", "Asia/Singapore", "Asia/Hong_Kong", "Asia/Tokyo", "Asia/Seoul", "Australia/Sydney",
  "Pacific/Auckland", "Africa/Johannesburg", "Africa/Cairo",
];

function zoneLabel(zone) {
  let offset = "";
  try {
    const part = new Intl.DateTimeFormat("en", { timeZone: zone, timeZoneName: "longOffset" })
      .formatToParts(new Date()).find((p) => p.type === "timeZoneName");
    offset = part ? ` (${part.value})` : "";
  } catch {
    offset = "";
  }
  return `${zone.replace(/_/g, " ")}${offset}`;
}

function cadenceSummary() {
  if (draft.cadence === "daily") return `Daily at ${draft.digest_time}, ${zoneLabel(draft.digest_tz)}`;
  if (draft.cadence === "weekly") return `Weekly on ${DAY_NAMES[draft.digest_day] || DAY_NAMES[0]} at ${draft.digest_time}, ${zoneLabel(draft.digest_tz)}`;
  return "Instant, as matches appear";
}

// Each count appears twice: small beside its button in the nav, large
// on a tile in the overview. One call writes both, so they cannot drift.
// They are set wherever the block they count gets filled, rather than by
// a request that asks for totals.
const counts = { skills: 0, alerts: 0, saved: 0 };

function setCount(key, n) {
  counts[key] = n;
  for (const id of ["stat-" + key, "tile-" + key]) {
    const el = $(id);
    if (el) el.textContent = String(n);
  }
  // A count of nothing is not information, so the nav badge goes away
  // rather than sitting there as a zero.
  const badge = $("stat-" + key);
  if (badge) badge.dataset.zero = n > 0 ? "0" : "1";
  paintOverview();
  paintSectionStatus(document.querySelector(".account-nav-link.active")?.getAttribute("href")?.slice(1) || "overview");
}

// Which Overview shows is decided here and nowhere else, from the same
// counts the nav badges use, so the two cannot disagree. All three done
// retires the steps outright rather than leaving a row of ticks.
function paintOverview() {
  const done = { skills: counts.skills > 0, alerts: counts.alerts > 0, saved: counts.saved > 0 };
  const all = done.skills && done.alerts && done.saved;

  // The checklist goes when there is nothing left on it. A row of ticks
  // is a list of things you cannot do any more.
  const setup = $("acct-setup");
  if (setup) setup.hidden = all;

  // A done row reads as struck through at 60%, and only the first one
  // still outstanding carries a button: three buttons is three
  // decisions, and the point of a checklist is that there is one.
  let offered = false;
  for (const [key, isDone] of Object.entries(done)) {
    const row = document.querySelector(`.acct-check-row[data-step="${key}"]`);
    if (!row) continue;
    row.classList.toggle("done", isDone);
    const btn = row.querySelector(".acct-check-do");
    if (btn) btn.hidden = isDone || offered;
    if (!isDone) offered = true;
  }
}

// A summary, not the whole list. Forty chips made the overview mostly
// chips; the rest are one click away in Skills, which is where
// they can actually be changed.
const TAGS_SHOWN = 7;

function paintSkillTags() {
  const host = $("acct-tags");
  if (!host) return;
  if (!draft.skills.length) {
    host.innerHTML = '<span class="acct-none">No skills yet. Read a resume in Skills.</span>';
    return;
  }
  const rest = draft.skills.length - TAGS_SHOWN;
  host.innerHTML =
    draft.skills.slice(0, TAGS_SHOWN)
      .map((sk) => `<span class="acct-tag">${escapeHtml(sk)}</span>`).join("")
    + (rest > 0 ? `<a class="acct-more" href="#cv">+${rest} more</a>` : "");
}

// Which provider signed this reader in, and only when the token says so.
// Cognito puts an identities claim on a federated user, which today is
// Google alone: GitHub runs through a custom auth Lambda on a native
// user (see github_auth_handler.py, which sets email and nothing else)
// and email OTP is native too, so neither is distinguishable from the
// other here. Rather than label those two with a guess, this says
// nothing about them.
const PROVIDER_MARKS = {
  Google: '<svg viewBox="0 0 18 18" width="13" height="13" aria-hidden="true"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z"/><path fill="#FBBC05" d="M3.964 10.71c-.18-.54-.282-1.117-.282-1.71s.102-1.17.282-1.71V4.958H.957C.348 6.173 0 7.548 0 9s.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/></svg>',
};

function paintProvider(idToken) {
  const el = $("account-provider");
  if (!el) return;
  let name = "";
  try {
    const claims = JSON.parse(atob(idToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    // Cognito writes this as a JSON string on some flows and an array on
    // others, so both shapes are read.
    let ids = claims.identities;
    if (typeof ids === "string") ids = JSON.parse(ids);
    name = (Array.isArray(ids) && ids[0] && ids[0].providerName) || "";
  } catch {
    return;
  }
  if (!PROVIDER_MARKS[name]) return;
  el.innerHTML = PROVIDER_MARKS[name] + `<span>Signed in with ${escapeHtml(name)}</span>`;
  el.hidden = false;
}

async function loadProfile() {
  try {
    const data = await authedFetch("/me/profile");
    return { profile: data.profile || EMPTY_PROFILE, skill_spec: data.skill_spec || null };
  } catch {
    // A profile that will not load is not worth blocking the page for;
    // the alerts below may still work, and an empty form is honest. The
    // pickers just have nothing to offer until the next load.
    return { profile: { ...EMPTY_PROFILE }, skill_spec: null };
  }
}

// The CV analyser. The file is read in this tab and never uploaded:
// pdf.js is vendored next door, the skill terms come down with the
// profile, and the only thing that ever reaches the server is the list
// of skills someone chooses to save.
//
// That is why the matching runs here rather than in the API. It uses the
// same terms probe.py tags every job description with, so a skill found
// in a CV is by construction a skill a listing can carry.

// The rules arrive with the profile (skills.spec() in the API) and the
// engine is frontend/cv_skills.js, the browser twin of the one that tags
// jobs. See that file and api/skills.py for how matching works.
let skillSpec = null;

// Strongest evidence first: when a CV yields more than the profile's 40,
// the ones it mentions most are the ones kept.
function skillsIn(text) {
  if (!skillSpec || !window.CvSkills) return [];
  return CvSkills.extract(text, skillSpec)
    .sort((a, b) => b.count - a.count || a.index - b.index)
    .map((d) => d.label);
}

async function textFromPdf(file) {
  // Imported on demand: a 330KB parser should not load for someone who
  // came to edit an alert.
  const pdfjs = await import("/vendor/pdfjs/pdf.min.mjs");
  pdfjs.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/pdf.worker.min.mjs";
  return CvSkills.textFromPdf(pdfjs, await file.arrayBuffer());
}

async function readCv(file) {
  if (file.type === "application/pdf" || /\.pdf$/i.test(file.name)) return textFromPdf(file);
  return file.text();
}

function paintChips() {
  const host = $("cv-chips");
  host.innerHTML = draft.skills.map((s) => `
    <button type="button" class="cv-chip" data-skill="${escapeHtml(s)}" title="Remove">
      ${escapeHtml(s)}<span aria-hidden="true">&times;</span>
    </button>`).join("");

  host.querySelectorAll(".cv-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      draft.skills = draft.skills.filter((s) => s !== chip.dataset.skill);
      paintChips();
      paintMatchLink();
    });
  });
  paintMatchLink();
  scheduleMatches();
}

// The three best matches for the skills on file. This block existed as
// empty markup and nothing ever filled it, so Overview has been showing
// a heading over nothing since it was added.
//
// The board's own ranking, asked for directly: skills= orders rather
// than filters, which is why it goes to /jobs with sort=match and why
// three rows cost the same query the board pays for fifty.
// Removing a chip repaints, and a match query is the board's ranking
// sort: 1.8s of real work. Removing four skills should cost one.
let matchesTimer = 0;
function scheduleMatches() {
  clearTimeout(matchesTimer);
  matchesTimer = setTimeout(loadMatches, 500);
}

// Cached for ten minutes, keyed by the exact question asked. The board's
// ranking sort is 1.8s of real work over a million rows, and this page
// was paying it again on every visit and every tab switch for an answer
// that cannot have changed. sessionStorage, not localStorage: it is a
// convenience for this sitting, not a record.
const MATCHES_TTL_MS = 10 * 60 * 1000;
const MATCHES_KEY = "iljobs_acct_matches";

function cachedMatches(key) {
  try {
    const hit = JSON.parse(sessionStorage.getItem(MATCHES_KEY) || "null");
    if (hit && hit.key === key && Date.now() - hit.at < MATCHES_TTL_MS) return hit.jobs;
  } catch {
    // Private browsing, or someone else's JSON in our key.
  }
  return null;
}

async function loadMatches() {
  const host = $("acct-matches");
  if (!host) return;
  const skills = draft.skills;
  if (!skills.length) {
    host.innerHTML = '<p class="acct-matches-empty">Add skills to see matches.</p>';
    return;
  }
  // No country. The analyser reads skills and nothing else, so nothing
  // here knows where the reader is, and there is no preference to ask
  // either: israel_only defaults to true for every profile ever created
  // and the account page has no control for it, so reading it would
  // have shown a developer in Germany three Israeli jobs and called
  // them their best matches. Narrowing by place is the board's job, and
  // "See all on the board" is the way to it.
  const q = new URLSearchParams({
    skills: skills.join(","), sort: "match", dir: "asc", limit: "3", count: "skip",
  }).toString();

  const cached = cachedMatches(q);
  if (cached) return paintMatches(cached, skills);
  host.innerHTML = '<p class="acct-matches-empty">Looking…</p>';
  try {
    const data = await getJSON(`/jobs?${q}`);
    const jobs = data.jobs || [];
    try {
      sessionStorage.setItem(MATCHES_KEY, JSON.stringify({ key: q, at: Date.now(), jobs }));
    } catch {
      // Quota or private browsing. The list still draws.
    }
    paintMatches(jobs, skills);
  } catch {
    // One failed panel is not worth an error banner on a page whose
    // other four sections are fine.
    host.innerHTML = '<p class="acct-matches-empty">Could not load matches.</p>';
  }
}

function paintMatches(jobs, skills) {
  const host = $("acct-matches");
  if (!host) return;
  if (!jobs.length) {
    host.innerHTML = '<p class="acct-matches-empty">Nothing matches those skills yet.</p>';
    return;
  }
  const want = new Set(skills);
  host.innerHTML = jobs.map((j) => {
    const listed = (j.skills || "").split(",").filter(Boolean);
    const hit = listed.filter((sk) => want.has(sk)).length;
    return `
      <a class="acct-match" href="/job/${encodeURIComponent(j.id)}">
        ${companyLogoImg(j.company_domain, 40, "listing", j.logo_url)}
        <span class="acct-match-text">
          <span class="acct-match-title">${escapeHtml(j.title)}</span>
          <span class="acct-match-meta">${escapeHtml(j.company_name || j.company_domain || "")}${
            j.location ? " · " + escapeHtml(j.location) : ""}</span>
        </span>
        <span class="acct-match-score">${hit} of ${want.size} skills</span>
      </a>`;
  }).join("");
}

function paintProfile(profile) {
  draft.skills = [...(profile.skills || [])];
  draft.seniority = profile.seniority || "";
  draft.workplace = [...(profile.workplace || [])];
  draft.israel_only = profile.israel_only !== false;
  draft.country = [...(profile.country || [])];
  draft.city = [...(profile.city || [])];
  draft.cadence = profile.cadence || "instant";
  draft.digest_time = profile.digest_time || "09:00";
  draft.digest_tz = profile.digest_tz || "Asia/Jerusalem";
  draft.digest_day = Number.isInteger(profile.digest_day) ? profile.digest_day : 0;
  // What Cancel goes back to: the last thing the server confirmed.
  savedPrefs = { workplace: [...draft.workplace], country: [...draft.country], city: [...draft.city], cadence: draft.cadence,
                 digest_time: draft.digest_time, digest_tz: draft.digest_tz, digest_day: draft.digest_day };
  if (draft.skills.length) {
    $("cv-result").hidden = false;
    paintChips();
  }
  paintMatchLink();
  paintPreferences();
}

// The Preferences section. Each row is one thing: its head shows the
// value in a line, opening it shows the control for that one value and
// a Save. The pickers are app.js's own, the same ones the alert form
// uses, so a place or a workplace means the same thing in both.
let prefMsWorkplace = null;
let prefMsLocation = null;
let savedPrefs = { workplace: [], country: [], city: [], cadence: "instant", digest_time: "09:00", digest_tz: "Asia/Jerusalem", digest_day: 0 };
// The place labels, once the facets are in; codes until then.
let placeLabels = {};

function wirePreferences() {
  if (!$("pref-item-workplace") || typeof createMultiSelect !== "function") return;
  prefMsWorkplace = createMultiSelect("pref-ms-workplace", {
    placeholder: "Workplace",
    options: Object.entries(WORKPLACE_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => { draft.workplace = values; },
  });
  prefMsLocation = createLocationSelect("pref-ms-location", {
    placeholder: "Locations",
    onChange: ({ countries, cities }) => { draft.country = countries; draft.city = cities; },
  });
  // The options arrive after the profile did, and setOptions drops any
  // selection it does not know, so the selection is painted again once
  // they are in.
  getStaticFacets("verified")
    .then((facets) => {
      const rows = normalizeLocationFacets(facets.locations);
      placeLabels = {};
      rows.forEach((c) => {
        placeLabels[c.value] = c.label || c.value;
        (c.cities || []).forEach((t) => { placeLabels[t.value] = t.label || t.value; });
      });
      prefMsLocation.setOptions(rows);
      paintPreferences();
    })
    .catch(() => {});
  document.querySelectorAll('input[name="pref-cadence"]').forEach((r) => {
    r.addEventListener("change", () => { if (r.checked) { draft.cadence = r.value; paintWhenControls(); } });
  });
  // The time and the zone are one value shown in two rows, so either
  // row's controls write the same fields and the other follows.
  const zones = [...ZONES];
  const here = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return null; } })();
  if (here && !zones.includes(here)) zones.unshift(here);
  document.querySelectorAll("#preferences .pref-tz").forEach((sel) => {
    sel.innerHTML = zones.map((z) => `<option value="${z}">${zoneLabel(z)}</option>`).join("");
    sel.addEventListener("change", () => { draft.digest_tz = sel.value; paintWhenControls(); });
  });
  document.querySelectorAll("#preferences .pref-time").forEach((inp) => {
    inp.addEventListener("change", () => { if (/^\d\d:\d\d$/.test(inp.value)) { draft.digest_time = inp.value; paintWhenControls(); } });
  });
  $("pref-day").addEventListener("change", () => { draft.digest_day = Number($("pref-day").value); });

  // One row open at a time. Opening a row is not a commitment: Cancel
  // and closing both put the draft back to what was last saved.
  document.querySelectorAll("#preferences .pref-head").forEach((head) => {
    head.addEventListener("click", () => {
      const it = head.closest(".pref-item");
      const open = !it.classList.contains("open");
      document.querySelectorAll("#preferences .pref-item.open").forEach((o) => { if (o !== it) closePrefItem(o, true); });
      if (open) {
        it.classList.add("open");
        head.setAttribute("aria-expanded", "true");
        it.querySelector(".pref-editor").hidden = false;
      } else {
        closePrefItem(it, true);
      }
    });
  });
  document.querySelectorAll("#preferences .pref-cancel").forEach((btn) => {
    btn.addEventListener("click", () => closePrefItem(btn.closest(".pref-item"), true));
  });
  document.querySelectorAll("#preferences .pref-save").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      // Held before the await: currentTarget is null once dispatch ends.
      const button = e.currentTarget;
      const key = button.dataset.pref;
      const it = button.closest(".pref-item");
      button.classList.add("btn-busy");
      try {
        const res = await authedFetch("/me/profile", { method: "PUT", body: JSON.stringify(draft) });
        paintProfile(res.profile || res);
        paintSectionStatus("preferences");
        closePrefItem(it, false);
      } catch (err) {
        setStatus(`pref-status-${key}`, err.message || "Could not save.", true);
      } finally {
        button.classList.remove("btn-busy");
      }
    });
  });
}

function closePrefItem(it, revert) {
  if (!it) return;
  if (revert) {
    draft.workplace = [...savedPrefs.workplace];
    draft.country = [...savedPrefs.country];
    draft.city = [...savedPrefs.city];
    draft.cadence = savedPrefs.cadence;
    draft.digest_time = savedPrefs.digest_time;
    draft.digest_tz = savedPrefs.digest_tz;
    draft.digest_day = savedPrefs.digest_day;
    paintPreferences();
  }
  it.classList.remove("open");
  it.querySelector(".pref-head").setAttribute("aria-expanded", "false");
  it.querySelector(".pref-editor").hidden = true;
  const status = it.querySelector(".account-status");
  if (status) status.textContent = "";
}

function placeSummary() {
  const names = [...draft.country, ...draft.city].map((v) => placeLabels[v] || v);
  if (!names.length) return "Anywhere";
  return names.length > 3 ? `${names.slice(0, 3).join(", ")} and ${names.length - 3} more` : names.join(", ");
}

function paintPreferences() {
  if (prefMsWorkplace) prefMsWorkplace.setSelected(draft.workplace);
  if (prefMsLocation) prefMsLocation.setSelected(draft.country, draft.city);
  document.querySelectorAll('input[name="pref-cadence"]').forEach((r) => { r.checked = r.value === (draft.cadence || "instant"); });
  paintWhenControls();
  const sum = (key, text) => { const el = $(`pref-sum-${key}`); if (el) el.textContent = text; };
  sum("workplace", draft.workplace.length ? draft.workplace.map((w) => WORKPLACE_LABELS[w] || w).join(", ") : "Any");
  sum("location", placeSummary());
  sum("cadence", cadenceSummary());
}

// The time, zone and day controls: both rows show the one value, and
// only the chosen row's controls are live.
function paintWhenControls() {
  document.querySelectorAll("#preferences .pref-time").forEach((inp) => { inp.value = draft.digest_time; });
  document.querySelectorAll("#preferences .pref-tz").forEach((sel) => {
    if (![...sel.options].some((o) => o.value === draft.digest_tz)) sel.add(new Option(zoneLabel(draft.digest_tz), draft.digest_tz));
    sel.value = draft.digest_tz;
  });
  const day = $("pref-day");
  if (day) day.value = String(draft.digest_day);
  document.querySelectorAll("#preferences .pref-when").forEach((span) => {
    const live = span.dataset.for === draft.cadence;
    span.classList.toggle("off", !live);
    span.querySelectorAll("input, select").forEach((c) => { c.disabled = !live; });
  });
}

// The line under the section title, in the words the rows use.
function prefsSummary() {
  const bits = [];
  if (draft.workplace.length) bits.push(draft.workplace.map((w) => WORKPLACE_LABELS[w] || w).join(", "));
  const places = draft.country.length + draft.city.length;
  if (places) bits.push(`${places} ${places === 1 ? "place" : "places"}`);
  bits.push({ daily: `daily at ${draft.digest_time}`, weekly: `weekly on ${DAY_NAMES[draft.digest_day] || "Monday"}` }[draft.cadence] || "instant alerts");
  return bits.join(" · ");
}

// The profile is expressible as an ordinary board search, which is the
// point of validating it against the same vocabulary the filters use.
// So "see my matches" is a link, not a feature.
function paintMatchLink() {
  const p = new URLSearchParams();
  // skills=, not q=. q is a single substring match against title and
  // company, so a dozen skills joined with spaces asked the board for
  // that exact phrase and found nothing, every time. See
  // profile_to_filter and job_filters.wanted_skills.
  if (draft.skills.length) p.set("skills", draft.skills.join(","));
  if (draft.seniority) p.set("seniority", draft.seniority);
  if (draft.workplace.length) p.set("workplace", draft.workplace.join(","));
  if (draft.country.length) p.set("country", draft.country.join(","));
  if (draft.city.length) p.set("city", draft.city.join(","));
  // Same reasoning as loadMatches: israel_only is a legacy flag with no
  // control behind it and a default of true, so sending it would filter
  // every reader's matches to one country none of them chose.
  const link = $("profile-matches");
  if (link) link.href = "/board?" + p.toString();
  setCount("skills", draft.skills.length);
  paintSkillTags();
}

async function takeCvFile(file) {
  $("cv-filename").textContent = file.name;
  setStatus("cv-status", "Reading…");
  try {
    const text = await readCv(file);
    // The rules come down with the profile. When that request failed
    // there is nothing to match against, and every CV read as empty:
    // a page whose profile would not load told people their CV had no
    // skills in it, which is a different and much worse thing to say
    // than "this did not load". Say which one it is.
    if (!skillSpec) {
      setStatus("cv-status", "The skill list did not load, so nothing can be matched. Reload the page and try again.", true);
      return;
    }
    const found = skillsIn(text);
    // Merge rather than replace: someone who analyses a second CV, or
    // has already added a skill by hand, should not silently lose it.
    draft.skills = [...new Set([...draft.skills, ...found])].slice(0, 40);
    $("cv-result").hidden = false;
    paintChips();
    setStatus("cv-status", found.length
      ? `Found ${found.length} skill${found.length === 1 ? "" : "s"}.`
      // No text at all is a scanned image, not a CV without skills.
      : text.trim()
        ? "No known skills found in that file."
        : "No text in that file. A scanned image cannot be read.");
  } catch (err) {
    setStatus("cv-status", "Could not read that file. PDF or plain text.", true);
  }
}

function wireProfile() {
  const input = $("cv-file");
  const zone = $("cv-drop");

  $("cv-analyze").addEventListener("click", () => input.click());

  input.addEventListener("change", async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      await takeCvFile(file);
    } finally {
      // So picking the same file twice still fires a change event.
      input.value = "";
    }
  });

  // Dropping one. The zone has been called cv-drop since it was a row
  // with a button in it; now it is the size of a target and behaves
  // like one. preventDefault on dragover is what tells the browser this
  // element will take the file instead of navigating to it.
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => {
    e.preventDefault();
    zone.classList.add("dragging");
  }));
  zone.addEventListener("dragleave", (e) => {
    // dragleave fires on every child crossed on the way in, so the
    // highlight only drops when the pointer has left the zone itself.
    if (!zone.contains(e.relatedTarget)) zone.classList.remove("dragging");
  });
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("dragging");
    const file = e.dataTransfer?.files?.[0];
    if (file) takeCvFile(file);
  });

  $("profile-save").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.classList.add("btn-busy");
    try {
      const res = await authedFetch("/me/profile", { method: "PUT", body: JSON.stringify(draft) });
      // Repaint from the response, not the draft: the server drops
      // anything it does not recognise, and the page should show what
      // was actually stored rather than what was asked for.
      paintProfile(res.profile || res);
      setStatus("profile-status", "Saved.");
    } catch (err) {
      setStatus("profile-status", err.message || "Could not save.", true);
    } finally {
      btn.classList.remove("btn-busy");
    }
  });
}

// The page's own nav. It opens a section rather than scrolling to one,
// so the page is never longer than the thing being read.
//
// Two shapes, one state. On a wide screen the nav is always there and
// exactly one section is open beside it. On a phone the nav is the
// whole screen until a section is opened, and Back closes it again,
// which is why the open section can be nothing at all below 960px and
// never can above it.
// One head for every section, so the five of them cannot drift out of
// line with each other. The status line is filled by whatever owns the
// section; the action is the one primary thing that section offers.
const SECTION_HEAD = {
  overview: { title: "Overview" },
  skills: { title: "Skills", action: '<button type="button" class="btn btn-small" id="acct-do-cv">Scan a CV</button>' },
  alerts: { title: "Alerts", action: '<a class="btn btn-small" href="#alerts">New alert</a>' },
  saved: { title: "Saved jobs", action: '<a class="btn ghost btn-small" href="/board">Open the board</a>' },
  preferences: { title: "Preferences" },
  settings: { title: "Settings" },
};

function paintSectionHead(id) {
  const spec = SECTION_HEAD[id] || SECTION_HEAD.overview;
  const title = $("acct-title");
  const action = $("acct-action");
  if (title) title.textContent = spec.title;
  if (action) action.innerHTML = spec.action || "";
  // Scan a CV is the file input the Skills section already owns, so the
  // header button forwards to it rather than holding a second one.
  const cv = $("acct-do-cv");
  if (cv) cv.addEventListener("click", () => $("cv-file")?.click());
  paintSectionStatus(id);
}

// The line under the title: what this section currently holds, in the
// same words its own count uses.
function paintSectionStatus(id) {
  const el = $("acct-status");
  if (!el) return;
  const n = (k, one, many) => `${counts[k]} ${counts[k] === 1 ? one : many}`;
  const text = {
    overview: "",
    skills: counts.skills ? n("skills", "skill", "skills") + " matched against every listing" : "No skills yet",
    alerts: counts.alerts ? n("alerts", "alert", "alerts") : "No alerts yet",
    saved: counts.saved ? n("saved", "saved job", "saved jobs") : "Nothing saved yet",
    preferences: prefsSummary(),
    settings: "",
  }[id] || "";
  el.textContent = text;
}

function wireAccountNav() {
  const layout = $("account-body");
  const page = document.querySelector(".account-shell");
  const back = $("account-back");
  const panels = [...document.querySelectorAll(".account-nav-link")]
    .map((link) => ({ link, id: link.getAttribute("href").slice(1), el: document.querySelector(link.getAttribute("href")) }))
    .filter((p) => p.el);
  if (!panels.length) return;

  // A section is always open, at every width. The phone used to leave
  // nothing open and show the nav as a screen of its own; the nav is a
  // strip of tabs there now, and tabs with nothing under them is the
  // blank content people reported.
  let open = null;

  const paint = () => {
    panels.forEach((p) => {
      p.el.classList.toggle("is-active", p.id === open);
      p.link.classList.toggle("active", p.id === open);
      p.link.setAttribute("aria-current", p.id === open ? "true" : "false");
    });
    layout.classList.toggle("section-open", !!open);
    if (page) page.classList.toggle("section-open", !!open);
    paintSectionHead(open || "overview");
  };

  // The hash is the section, so a link to /account#alerts opens there
  // and the browser's own Back walks out of a section on a phone the
  // same way the button does.
  const show = (id, record) => {
    open = id;
    paint();
    if (record) {
      const url = id ? "#" + id : location.pathname;
      if (history.state && history.state.acct === id) history.replaceState({ acct: id }, "", url);
      else history.pushState({ acct: id }, "", url);
    }
    window.scrollTo(0, 0);
  };

  // The nav, and everything else that names a section: the overview's
  // counts and its "+N more". One handler, so a link added later behaves
  // like the nav without knowing anything about it.
  const byId = new Map(panels.map((p) => [p.id, p]));
  layout.addEventListener("click", (e) => {
    const a = e.target.closest('a[href^="#"]');
    if (!a || !layout.contains(a)) return;
    const p = byId.get(a.getAttribute("href").slice(1));
    if (!p) return;
    e.preventDefault();
    show(p.id, true);
  });
  if (back) back.addEventListener("click", () => show(panels[0].id, true));

  addEventListener("popstate", () => {
    const id = location.hash.slice(1);
    open = panels.some((p) => p.id === id) ? id : panels[0].id;
    paint();
  });

  const asked = location.hash.slice(1);
  const landing = panels.find((p) => p.id === asked);
  show(landing ? landing.id : panels[0].id, false);
}

// Alerts are app.js's own renderAlertsList and wireAlertCreateForm,
// pointed at markup on this page with the same ids. A second copy of a
// form with six filter pickers is exactly the kind of duplication that
// drifts, and the board's version already handles create, edit, pause
// and delete.
//
// populateAlertFilterOptions reads latestStats for the department and
// location lists, which only the board itself normally fills in, so this
// page fetches the same stats the board does.
async function wireAlerts() {
  // renderAlertsList is app.js's, and it is called again after every
  // create, edit, pause and delete. Counting inside it is the only hook
  // that catches all of those without a second copy of the list here.
  const paintList = renderAlertsList;
  renderAlertsList = (list) => {
    setCount("alerts", (list || []).length);
    paintList(list);
  };

  // The list first, and on its own. It used to come last, behind a
  // stats download and the whole create form, so anything slow or stuck
  // in either left "Loading..." on screen with nothing to say why. The
  // list only ever needed its own request.
  // authedFetch directly, not loadMyAlerts: that one swallows every
  // error and answers with an empty array, so a request that failed
  // rendered as "No alerts yet" and told the reader their alerts were
  // gone. Here the throw belongs to block(), which says so instead.
  renderAlertsList((await authedFetch("/me/alerts")).alerts || []);

  // The form and its pickers after, and their failures are theirs: an
  // alert you cannot create is not a reason to hide the ones you have.
  try {
    latestStats = await getStaticOrApi("/stats.json", "/stats");
  } catch {
    // The pickers fall back to whatever they can load on their own, and
    // the search box still works.
  }
  wireAlertCreateForm();
}

// The Settings row's theme button. It clicks the topbar's, which owns
// the switching and the storage key, and mirrors whatever label that
// leaves behind. The observer is what keeps the two in step when the
// theme is changed from the bar instead of from here.
function wireSettingsTheme() {
  const here = $("settings-theme");
  const bar = document.getElementById("theme-toggle");
  if (!here || !bar) return;
  const sync = () => {
    here.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "Light" : "Dark";
  };
  sync();
  here.addEventListener("click", () => bar.click());
  new MutationObserver(sync).observe(document.documentElement, { attributeFilter: ["data-theme"] });
}

function wireLeaving() {
  // This page is the one protected view on the site, so signing out
  // here leaves a signed-out reader looking at an account. Everywhere
  // else redraws in place, which is right, because everywhere else is
  // public.
  $("account-signout").addEventListener("click", () => {
    signOut();
    location.href = "/";
  });

  // A separate Confirm button rather than the same button changing its
  // own label. The destructive click then lands somewhere the pointer
  // was not already resting, and the first button keeps saying what it
  // does while the second says what happens next.
  const del = $("account-delete");
  const confirmBtn = $("account-delete-confirm");
  let armed = null;

  const disarm = () => {
    clearTimeout(armed);
    armed = null;
    confirmBtn.hidden = true;
  };

  del.addEventListener("click", () => {
    if (!confirmBtn.hidden) return disarm();
    confirmBtn.hidden = false;
    armed = setTimeout(disarm, 8000);
  });

  confirmBtn.addEventListener("click", async (e) => {
    // Cancel the auto-hide but leave the button on screen: hiding it
    // here would take the only thing showing progress away mid-delete.
    clearTimeout(armed);
    armed = null;
    const btn = e.currentTarget;
    btn.classList.add("btn-busy");
    try {
      const alerts = (await authedFetch("/me/alerts")).alerts || [];
      for (const a of alerts) {
        await authedFetch(`/me/alerts/${a.alert_id}`, { method: "DELETE" });
      }
      // An empty profile is the same shape as never having had one, so
      // this is a delete without needing a delete route.
      await authedFetch("/me/profile", {
        method: "PUT",
        body: JSON.stringify(EMPTY_PROFILE),
      });
      setStatus("account-status", "Deleted. Signing out.");
      setTimeout(signOut, 1200);
    } catch (err) {
      setStatus("account-status", err.message || "Could not delete.", true);
      btn.classList.remove("btn-busy");
      disarm();
    }
  });
}

// Saved listings.
//
// Two requests, not one: the account only stores the ids, because a copy
// of the listing itself would be stale the moment the job closed or
// moved. The ids come from the account and the listings come from the
// board's own endpoint, so what is shown here is always the live record.
//
// include_closed and include_outdated are both on deliberately. Someone
// who saved a job wants to know it closed far more than they want a
// tidy list, and the board hides closed rows by default.
async function loadSaved() {
  const host = $("saved-list");
  if (!host) return;
  let ids = [];
  try {
    const data = await authedFetch("/me/saved");
    ids = (data.saved || []).map((r) => r.job_id).filter(Boolean);
  } catch {
    host.innerHTML = '<p class="alerts-empty">Could not load your saved listings.</p>';
    return;
  }
  setCount("saved", ids.length);
  if (!ids.length) {
    host.innerHTML = '<p class="alerts-empty">No saved jobs yet.</p>';
    return;
  }
  let jobs = [];
  try {
    const q = new URLSearchParams({
      ids: ids.slice(0, 200).join(","),
      include_closed: "1",
      include_outdated: "1",
      limit: "200",
    });
    jobs = (await getJSON(`/jobs?${q}`)).jobs || [];
  } catch {
    host.innerHTML = '<p class="alerts-empty">Could not load your saved listings.</p>';
    return;
  }
  // The account knows the order they were starred in; the board returns
  // them in its own. Restore the reader's order, newest first.
  const rank = new Map(ids.map((id, i) => [id, i]));
  jobs.sort((a, b) => (rank.get(a.id) ?? 999) - (rank.get(b.id) ?? 999));
  paintSaved(jobs);
}

function paintSaved(jobs) {
  const host = $("saved-list");
  host.innerHTML = jobs.map((j) => {
    const where = [j.company_name || (j.company_domain || "").replace(/\.invalid$/, ""), j.location].filter(Boolean).join(" · ");
    return `
      <div class="alert-row" data-saved="${escapeHtml(j.id)}">
        <div class="saved-main">
          <a class="saved-title" href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener">${escapeHtml(j.title)}</a>
          ${j.closed_at ? '<span class="badge closed" title="This listing is no longer open">Closed</span>' : ""}
          <div class="saved-meta">${escapeHtml(where)}</div>
        </div>
        <button class="alert-delete" type="button" data-unsave="${escapeHtml(j.id)}" title="Remove from saved">✕</button>
      </div>`;
  }).join("");

  host.querySelectorAll("[data-unsave]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.unsave;
      btn.disabled = true;
      try {
        await authedFetch(`/me/saved/${encodeURIComponent(id)}`, { method: "DELETE" });
      } catch (err) {
        btn.disabled = false;
        // Next to the list, not in Settings, and it says what happened.
        // A bare "could not" on a button that looks idle is the same as
        // silence: the reader clicks it again.
        setStatus("saved-status", `Could not remove that listing: ${err.message}`, true);
        return;
      }
      // The board reads this same list out of localStorage, so removing
      // it here has to remove it there too, or the star would still be
      // lit the next time this reader opens the board in this browser.
      try {
        const key = "iljobs_starred";
        const local = new Set(JSON.parse(localStorage.getItem(key) || "[]"));
        local.delete(id);
        localStorage.setItem(key, JSON.stringify([...local]));
      } catch {
        // Private browsing or a full quota. The account is the record
        // that matters; this copy is a convenience.
      }
      const row = btn.closest("[data-saved]");
      if (row) row.remove();
      setCount("saved", $("saved-list").querySelectorAll("[data-saved]").length);
      if (!$("saved-list").querySelector("[data-saved]")) loadSaved();
    });
  });
}

// The signed-out half of the page. The board's topbar panel does the
// same job with the same functions from app.js; this one exists because
// the account page has no topbar auth slot to paint into (see
// renderAuthState), and telling a reader to go and sign in somewhere
// else is not an answer on the page called Account.
function wireAccountSignIn() {
  const err = $("acct-error");
  const fail = (msg) => { err.textContent = msg; err.hidden = false; };
  const clear = () => { err.hidden = true; };
  $("acct-google").addEventListener("click", () => { clear(); startGoogleSignIn(); });
  $("acct-github").addEventListener("click", () => { clear(); startGithubSignIn(); });
  $("acct-email-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clear();
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      await startEmailSignIn($("acct-email-input").value.trim());
      $("acct-email-form").hidden = true;
      $("acct-otp-form").hidden = false;
      $("acct-otp-input").focus();
    } catch (e2) {
      fail(e2.message || "Could not send a code. Try again.");
    } finally {
      btn.disabled = false;
    }
  });
  $("acct-otp-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clear();
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      await verifyEmailOtp($("acct-otp-input").value.trim());
      // The page reads its token once, at boot, and every section below
      // is built from it. Coming back through boot is simpler and more
      // honest than teaching each one to appear.
      location.reload();
    } catch (e2) {
      fail(e2.message === "CodeMismatchException" ? "Wrong code, try again." : e2.message || "Could not verify that code.");
    } finally {
      btn.disabled = false;
    }
  });
}

// Each block of this page fills itself, and one that cannot must not
// take the others down with it. bootAccount used to be a straight line
// of awaits, so a single throw anywhere in it stopped everything below:
// reported live as Alerts stuck on "Loading...", and in the same breath
// Saved jobs and Best matches empty, which was one fault wearing three
// costumes. A block that fails now says so in its own container and the
// next one still runs. The error goes to the console too, because the
// message on screen is for the reader and the stack is for me.
//
// A promise that never settles cannot be caught either, and there the
// reader just sees the loading placeholder forever. After fifteen
// seconds the container is declared stuck: the work may still land and
// overwrite the message, which is fine, but silence is not an outcome.
// The timers are armed when the page opens rather than when each block
// starts, because a block that never starts is the same failure and the
// worse one. Reported live 2026-09-24: a stalled token refresh upstream
// left Alerts and Saved jobs on "Loading..." with no message and nothing
// in the console, since neither block had been entered.
const STUCK_MS = 15000;
const LOADING_HOSTS = ["alerts-list", "saved-list"];
const stuckTimers = new Map();

function sayStuck(host, why) {
  const el = $(host);
  if (!el || !/Loading/i.test(el.textContent)) return;
  console.error(`account: ${host} ${why}`);
  el.innerHTML = `<p class="alerts-empty">This is taking longer than it should. Reloading the page usually fixes it.</p>`;
}

function watchForStuck() {
  for (const host of LOADING_HOSTS) {
    stuckTimers.set(host, setTimeout(() => sayStuck(host, "never filled"), STUCK_MS));
  }
}

async function block(name, host, fn) {
  // A container this page did not pre-arm gets its timer here.
  if (host && !stuckTimers.has(host)) {
    stuckTimers.set(host, setTimeout(() => sayStuck(host, "did not finish"), STUCK_MS));
  }
  try {
    return await fn();
  } catch (err) {
    console.error(`account: ${name} failed`, err);
    const el = $(host);
    if (el) el.innerHTML = `<p class="alerts-empty">This did not load. Reloading the page usually fixes it.</p>`;
    return null;
  } finally {
    clearTimeout(stuckTimers.get(host));
    stuckTimers.delete(host);
  }
}

async function bootAccount() {
  const tokens = getAuthTokens();
  if (!tokens?.id_token) return showSignedOut();
  $("account-body").hidden = false;
  const email = decodeJwtEmail(tokens.id_token) || "signed in";
  $("account-email").textContent = email;
  // The name when the token carries one, the address otherwise. The
  // sidebar shows both, so the name line must not repeat the email.
  const name = decodeJwtName(tokens.id_token);
  const nameEl = $("account-name");
  if (nameEl) nameEl.textContent = name || email;
  if (nameEl && !name) $("account-email").hidden = true;
  const settingsEmail = $("settings-email");
  if (settingsEmail) settingsEmail.textContent = email;
  // The same mark the board's account menu draws: Google's photo when
  // the token carries one, the first letter when it does not.
  $("account-avatar").innerHTML = avatarHtml(email, tokens.id_token);
  paintProvider(tokens.id_token);
  paintSkillTags();

  watchForStuck();

  await block("chrome", null, async () => {
    wireLeaving();
    wireAccountNav();
    wireSettingsTheme();
  });

  // Three independent fetches, started together. The profile used to be
  // awaited here on its own, ahead of the other two, and they need
  // nothing from it: one stalled request upstream meant the alerts and
  // saved blocks were never entered at all, so both lists sat on
  // "Loading..." for as long as the tab stayed open and neither
  // watchdog armed.
  await Promise.allSettled([
    block("profile", "cv-chips", async () => {
      // Order matters inside this block: the analyser runs on the rules
      // the server returns (skill_spec), so it cannot be wired before
      // they arrive.
      const loaded = await loadProfile();
      skillSpec = loaded.skill_spec || null;
      wireProfile();
      wirePreferences();
      paintProfile(loaded.profile);
    }),
    block("alerts", "alerts-list", wireAlerts),
    block("saved", "saved-list", loadSaved),
  ]);

  // The session can die between opening the page and the first request:
  // Cognito refusing a refresh token signs the reader out from under us.
  // Without this the page stayed up with every count at zero and every
  // list empty, which reads as "my data is gone" rather than "you are
  // signed out".
  if (!getAuthTokens()?.id_token) showSignedOut();
}

function showSignedOut() {
  const body = $("account-body");
  if (body) body.hidden = true;
  $("account-signedout").hidden = false;
  // The page behind it has nothing to show without a token, so it gives
  // up its own rule and heading while the dialog is up.
  document.body.classList.add("signin-open");
  wireAccountSignIn();
}

bootAccount();
