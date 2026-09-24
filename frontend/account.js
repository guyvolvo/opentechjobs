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
const draft = { skills: [], seniority: "", workplace: [], israel_only: true };

function setStatus(el, text, isError = false) {
  const node = $(el);
  if (!node) return;
  node.textContent = text;
  node.classList.toggle("error", !!isError);
  if (text) setTimeout(() => { node.textContent = ""; node.classList.remove("error"); }, 4000);
}

const EMPTY_PROFILE = { skills: [], seniority: null, workplace: [], israel_only: true };

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

async function loadMatches() {
  const host = $("acct-matches");
  if (!host) return;
  const skills = draft.skills;
  if (!skills.length) {
    host.innerHTML = '<p class="acct-matches-empty">Add skills to see matches.</p>';
    return;
  }
  host.innerHTML = '<p class="acct-matches-empty">Looking…</p>';
  try {
    const q = new URLSearchParams({
      skills: skills.join(","), sort: "match", dir: "asc",
      limit: "3", country: "IL", count: "skip",
    });
    const data = await getJSON(`/jobs?${q}`);
    const jobs = data.jobs || [];
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
  } catch {
    // One failed panel is not worth an error banner on a page whose
    // other four sections are fine.
    host.innerHTML = '<p class="acct-matches-empty">Could not load matches.</p>';
  }
}

function paintProfile(profile) {
  draft.skills = [...(profile.skills || [])];
  draft.seniority = profile.seniority || "";
  draft.workplace = [...(profile.workplace || [])];
  draft.israel_only = profile.israel_only !== false;
  if (draft.skills.length) {
    $("cv-result").hidden = false;
    paintChips();
  }
  paintMatchLink();
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
  if (draft.israel_only) p.set("israel_only", "1");
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

  const narrow = () => window.matchMedia("(max-width: 960px)").matches;
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
  back.addEventListener("click", () => show(null, true));

  addEventListener("popstate", () => {
    const id = location.hash.slice(1);
    open = panels.some((p) => p.id === id) ? id : null;
    if (!open && !narrow()) open = panels[0].id;
    paint();
  });

  // Coming back across the breakpoint from the phone's menu, where
  // nothing is open, into a layout that has no menu to show.
  addEventListener("resize", () => {
    if (!open && !narrow()) show(panels[0].id, false);
  }, { passive: true });

  const asked = location.hash.slice(1);
  const landing = panels.find((p) => p.id === asked);
  show(landing ? landing.id : (narrow() ? null : panels[0].id), false);
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
  renderAlertsList(await loadMyAlerts());

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
  const sync = () => { here.textContent = bar.textContent; };
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
        body: JSON.stringify({ skills: [], seniority: null, workplace: [], israel_only: true }),
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
async function block(name, host, fn) {
  // A promise that never settles cannot be caught, and the reader sees
  // the loading placeholder forever. After fifteen seconds the block is
  // declared stuck: the work may still land and overwrite this, which
  // is fine, but silence is not an outcome.
  const stuck = host && setTimeout(() => {
    const el = $(host);
    if (el && /Loading/i.test(el.textContent)) {
      console.error(`account: ${name} did not finish`);
      el.innerHTML = `<p class="alerts-empty">This is taking longer than it should. Reloading the page usually fixes it.</p>`;
    }
  }, 15000);
  try {
    return await fn();
  } catch (err) {
    console.error(`account: ${name} failed`, err);
    const el = $(host);
    if (el) el.innerHTML = `<p class="alerts-empty">This did not load. Reloading the page usually fixes it.</p>`;
    return null;
  } finally {
    clearTimeout(stuck);
  }
}

async function bootAccount() {
  const tokens = getAuthTokens();
  if (!tokens?.id_token) {
    $("account-signedout").hidden = false;
    // The page behind it has nothing to show without a token, so it
    // gives up its own rule and heading while the dialog is up.
    document.body.classList.add("signin-open");
    wireAccountSignIn();
    return;
  }
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

  await block("chrome", null, async () => {
    wireLeaving();
    wireAccountNav();
    wireSettingsTheme();
  });

  // Order matters: the analyser runs on the rules the server returns
  // (skill_spec), so it cannot be wired before they arrive.
  const loaded = await loadProfile();
  skillSpec = loaded.skill_spec || null;
  await block("profile", "cv-chips", async () => {
    wireProfile();
    paintProfile(loaded.profile);
  });

  // Not awaited by each other. Alerts is the slow one and saved jobs has
  // nothing to do with it.
  await Promise.allSettled([
    block("alerts", "alerts-list", wireAlerts),
    block("saved", "saved-list", loadSaved),
  ]);
}

bootAccount();
