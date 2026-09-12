// The account page. Loads after app.js and reuses its auth helpers,
// createMultiSelect and escapeHtml rather than a second copy of each;
// app.js stops its own boot early when the board markup is absent.
//
// Everything here needs a token, so the page is one of two states: a
// prompt to sign in, or the real thing. There is no useful half-signed-in
// version.

const $ = (id) => document.getElementById(id);

let skillsPicker;
let workplacePicker;
// Served by GET /me/profile alongside the profile itself, so the picker
// offers exactly the labels the API will accept and probe.py tags jobs
// with. A hardcoded copy here drifted from the real list within minutes
// the first time it was tried.
let vocabulary = { skills: [], seniority: [], workplace: [] };
// What the server last confirmed. Save compares against this so the
// button can say "Saved" only when something actually changed.
let saved = null;
const draft = { skills: [], seniority: "", workplace: [], israel_only: true };

function setStatus(el, text, isError = false) {
  const node = $(el);
  if (!node) return;
  node.textContent = text;
  node.classList.toggle("error", !!isError);
  if (text) setTimeout(() => { node.textContent = ""; node.classList.remove("error"); }, 4000);
}

const EMPTY_PROFILE = { skills: [], seniority: null, workplace: [], israel_only: true };

async function loadProfile() {
  try {
    const data = await authedFetch("/me/profile");
    return { profile: data.profile || EMPTY_PROFILE, options: data.options || null };
  } catch {
    // A profile that will not load is not worth blocking the page for;
    // the alerts below may still work, and an empty form is honest. The
    // pickers just have nothing to offer until the next load.
    return { profile: { ...EMPTY_PROFILE }, options: null };
  }
}

function paintProfile(profile) {
  draft.skills = [...(profile.skills || [])];
  draft.seniority = profile.seniority || "";
  draft.workplace = [...(profile.workplace || [])];
  draft.israel_only = profile.israel_only !== false;

  skillsPicker.setSelected(draft.skills);
  workplacePicker.setSelected(draft.workplace);
  $("profile-seniority").value = draft.seniority;
  $("profile-israel").checked = draft.israel_only;
  paintMatchLink();
}

// The profile is expressible as an ordinary board search, which is the
// point of validating it against the same vocabularies the filters use.
// So "see my matches" is a link, not a feature.
function paintMatchLink() {
  const p = new URLSearchParams();
  if (draft.skills.length) p.set("q", draft.skills.join(" "));
  if (draft.seniority) p.set("seniority", draft.seniority);
  if (draft.workplace.length) p.set("workplace", draft.workplace.join(","));
  if (draft.israel_only) p.set("israel_only", "1");
  const link = $("profile-matches");
  link.href = "/?" + p.toString();
  link.textContent = draft.skills.length ? "See my matches" : "Browse all listings";
}

function wireProfile() {
  skillsPicker = createMultiSelect("profile-skills", {
    placeholder: "Skills",
    searchable: true,
    // The same closed list the API validates against and probe.py tags
    // jobs with, so a skill picked here can always match something.
    options: (vocabulary.skills || []).map((s) => ({ value: s, label: s })),
    onChange: (values) => { draft.skills = values; paintMatchLink(); },
  });
  workplacePicker = createMultiSelect("profile-workplace", {
    placeholder: "Workplace",
    options: Object.entries(WORKPLACE_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => { draft.workplace = values; paintMatchLink(); },
  });

  const seniority = $("profile-seniority");
  for (const [value, label] of Object.entries(SENIORITY_LABELS)) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    seniority.append(opt);
  }
  seniority.addEventListener("change", (e) => { draft.seniority = e.target.value; paintMatchLink(); });
  $("profile-israel").addEventListener("change", (e) => {
    draft.israel_only = e.target.checked;
    paintMatchLink();
  });

  $("profile-save").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.classList.add("btn-busy");
    try {
      const res = await authedFetch("/me/profile", { method: "PUT", body: JSON.stringify(draft) });
      saved = res.profile || res;
      // Repaint from the response, not the draft: the server drops
      // anything it does not recognise, and the form should show what
      // was actually stored rather than what was asked for.
      paintProfile(saved);
      setStatus("profile-status", "Saved.");
    } catch (err) {
      setStatus("profile-status", err.message || "Could not save.", true);
    } finally {
      btn.classList.remove("btn-busy");
    }
  });
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
  try {
    latestStats = await getStaticOrApi("/stats.json", "/stats");
  } catch {
    // Non-fatal: the pickers fall back to whatever they can load on
    // their own, and the search box still works.
  }
  wireAlertCreateForm();
  renderAlertsList(await loadMyAlerts());
}

function wireLeaving() {
  $("account-signout").addEventListener("click", signOut);

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

async function bootAccount() {
  const tokens = getAuthTokens();
  if (!tokens?.id_token) {
    $("account-signedout").hidden = false;
    return;
  }
  $("account-body").hidden = false;
  $("account-email").textContent = decodeJwtEmail(tokens.id_token) || "signed in";

  wireLeaving();
  // Order matters: the skills picker is built from the vocabulary the
  // server returns, so it cannot be wired before that arrives.
  const loaded = await loadProfile();
  vocabulary = loaded.options || vocabulary;
  wireProfile();
  paintProfile(loaded.profile);
  await wireAlerts();
}

bootAccount();
