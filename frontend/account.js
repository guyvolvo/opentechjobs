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
let skillTerms = [];
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
    return { profile: data.profile || EMPTY_PROFILE, skill_terms: data.skill_terms || [] };
  } catch {
    // A profile that will not load is not worth blocking the page for;
    // the alerts below may still work, and an empty form is honest. The
    // pickers just have nothing to offer until the next load.
    return { profile: { ...EMPTY_PROFILE }, skill_terms: [] };
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

let matchers = null;

function buildMatchers(terms) {
  // Mirrors probe.py's _SKILL_KEYWORDS: one case-insensitive,
  // word-bounded alternation per label. \b behaves the same either side
  // for the ASCII these needles are made of.
  return (terms || []).map(({ label, needles }) => ({
    label,
    re: new RegExp(
      "\\b(?:" + needles.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")\\b",
      "i",
    ),
  }));
}

function skillsIn(text) {
  // Ordered by where each one appears, like the tagger, so the list
  // reads the way the document does rather than the way our vocabulary
  // happens to be sorted.
  const hits = [];
  for (const { label, re } of matchers || []) {
    const m = re.exec(text);
    if (m) hits.push([m.index, label]);
  }
  hits.sort((a, b) => a[0] - b[0]);
  return hits.map(([, label]) => label);
}

async function textFromPdf(file) {
  // Imported on demand: a 330KB parser should not load for someone who
  // came to edit an alert.
  const pdfjs = await import("/vendor/pdfjs/pdf.min.mjs");
  pdfjs.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/pdf.worker.min.mjs";
  const doc = await pdfjs.getDocument({ data: await file.arrayBuffer() }).promise;
  const pages = [];
  for (let i = 1; i <= doc.numPages; i++) {
    const content = await (await doc.getPage(i)).getTextContent();
    pages.push(content.items.map((it) => it.str).join(" "));
  }
  return pages.join("\n");
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
    </button>`).join("") || '<span class="cv-none">No known skills found. The file may be an image scan.</span>';

  host.querySelectorAll(".cv-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      draft.skills = draft.skills.filter((s) => s !== chip.dataset.skill);
      paintChips();
      paintMatchLink();
    });
  });
  paintMatchLink();
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
  if (draft.skills.length) p.set("q", draft.skills.join(" "));
  if (draft.seniority) p.set("seniority", draft.seniority);
  if (draft.workplace.length) p.set("workplace", draft.workplace.join(","));
  if (draft.israel_only) p.set("israel_only", "1");
  const link = $("profile-matches");
  if (link) link.href = "/?" + p.toString();
}

function wireProfile() {
  matchers = buildMatchers(skillTerms);
  const input = $("cv-file");

  $("cv-analyze").addEventListener("click", () => input.click());

  input.addEventListener("change", async () => {
    const file = input.files?.[0];
    if (!file) return;
    $("cv-filename").textContent = file.name;
    setStatus("cv-status", "Reading…");
    try {
      const text = await readCv(file);
      const found = skillsIn(text);
      // Merge rather than replace: someone who analyses a second CV, or
      // has already added a skill by hand, should not silently lose it.
      draft.skills = [...new Set([...draft.skills, ...found])].slice(0, 20);
      $("cv-result").hidden = false;
      paintChips();
      setStatus("cv-status", found.length
        ? `Found ${found.length} skill${found.length === 1 ? "" : "s"}. Nothing was uploaded.`
        : "No known skills found. Nothing was uploaded.");
    } catch (err) {
      setStatus("cv-status", "Could not read that file. PDF or plain text.", true);
    } finally {
      // So picking the same file twice still fires a change event.
      input.value = "";
    }
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
  // Order matters: the matchers are built from the terms the server
  // returns, so the analyser cannot be wired before they arrive.
  const loaded = await loadProfile();
  skillTerms = loaded.skill_terms || [];
  wireProfile();
  paintProfile(loaded.profile);
  await wireAlerts();
}

bootAccount();
