// OpenTechJobs frontend. No framework, no build step, served straight
// from S3/CloudFront, runs as-shipped. Talks to the API at /api/*,
// same-origin (CloudFront routes /api/* to the Lambda).
//
// Starring a listing writes to localStorage first and always, because
// every render path reads that set synchronously, signed in or out.
// Signed in, the same set is mirrored to /me/saved, so a star set on
// this laptop is there on the phone too.

const API_BASE = "/api";
const STAR_KEY = "iljobs_starred";
const PAGE_SIZE = 50;

// Fixed vocabulary. Matches probe.py's _classify_seniority/Job.seniority
// exactly, not derived from the data, since it's a closed enum rather
// than free text like department/company.
const SENIORITY_LABELS = {
  intern: "Intern",
  junior: "Junior",
  mid: "Mid",
  senior: "Senior",
  staff: "Staff",
  principal: "Principal",
  lead: "Lead",
  manager: "Manager",
  director: "Director",
  exec: "Executive",
};

// Matches probe.py's Job.workplace_type exactly; see its docstring.
const WORKPLACE_LABELS = {
  remote: "Remote",
  hybrid: "Hybrid",
  onsite: "Onsite",
};

const state = {
  // One box. Every word must appear somewhere in the listing, quotes
  // keep a phrase whole. It replaced two boxes, q and keywords, that
  // asked visibly different questions with nothing on screen saying so:
  // one read the description and the other did not, one was AND and the
  // other a single substring, and the separator was a semicolon. The API
  // still answers both, because saved alerts carry them.
  search: "",
  // The CV match, from /account: canonical skill labels, OR-matched
  // and ranked by overlap. Deliberately not folded into `keywords` or
  // `q` -- those ask "which jobs demand all of this" and "which titles
  // contain this exact string", and a CV is neither question.
  skills: [],
  department: [], // labeled "Category" in the UI; backend field stays "department"
  seniority: [],
  company: [], // multi-select; also set via clicking a company in the market panels
  // Both halves of the one Locations dropdown, and both geocoded. They
  // are separate filters that AND together on the server, which is why
  // ticking a country here does not tick its cities: country=IL with
  // city=Berlin is a legitimate way to ask for nothing, not a bug.
  country: [], // ISO 3166-1 alpha-2 codes; a job can carry several
  city: [], // canonical city names, as the locations facet spells them
  workplace: [], // remote|hybrid|onsite
  confidence: "all", // no confidence filter in the UI; shown inline via badge instead
  max_age_days: "", // "" = any time; else days-since-posting cutoff, straight into the API param of the same name
  starred_only: false,
  sort: "age",
  dir: "asc", // newest first by default
  offset: 0,
};

// Assigned once in wireFilters(); referenced by refreshFacetOptions() and
// the market panels' "click a company" handler.
let msDepartment, msSeniority, msCompany, msLocation, msWorkplace;

// The job detail panel's current selection. Not part of `state` above
// since it's ephemeral UI, never an API param. Survives filter changes
// (doesn't get yanked closed), just loses its row highlight if that job
// scrolls off the current page.
let selectedJobId = null;

// The detail panel is a sheet over the board at every width: right-hand
// above 960px, full-screen below (see style.css). This query is only for
// the swipe-to-close gesture, which is written against the bottom
// sheet's translateY and would fight the side sheet's translateX. It
// mirrors the real media query in style.css.
const MOBILE_SHEET_QUERY = window.matchMedia("(max-width: 960px)");

// Guards the close-then-reopen race: closeJobDetail's hidden=true is
// delayed to let the slide-out transition finish, so picking a
// different job mid-close would otherwise let that stale timeout yank
// the just-opened panel back to hidden a moment later.
let jobDetailCloseTimer = null;

// storage

function getStarred() {
  try {
    return new Set(JSON.parse(localStorage.getItem(STAR_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function toggleStar(id) {
  const s = getStarred();
  s.has(id) ? s.delete(id) : s.add(id);
  localStorage.setItem(STAR_KEY, JSON.stringify([...s]));
  return s;
}

function setStarred(ids) {
  localStorage.setItem(STAR_KEY, JSON.stringify([...ids]));
}

// Repaints the star controls for one job wherever they are on screen,
// which is the table row's button and, when that job is the one the
// detail panel has open, its Save button too. Used by the paths that
// change the set without going through a click, so the glyph still
// follows the data.
function paintStar(id, starredSet) {
  const on = starredSet.has(id);
  const rowBtn = document.querySelector(`[data-star="${id}"].star-btn`);
  if (rowBtn) {
    rowBtn.classList.toggle("on", on);
    rowBtn.textContent = on ? "★" : "☆";
  }
  syncDetailStarButton(id, starredSet);
}

// The server mirror. The click handlers never await this. A star is a
// localStorage toggle that has always felt instant, and making it wait
// on a Lambda round-trip would be a visible downgrade for the one thing
// on this board that never had to wait.
function pushStar(id, saved) {
  if (!getAuthTokens()) return; // signed out, and then localStorage is the whole story, exactly as before
  authedFetch(`/me/saved/${encodeURIComponent(id)}`, { method: saved ? "PUT" : "DELETE" })
    .catch(() => revertStar(id, saved));
}

// A write that did not land must not leave this browser quietly
// disagreeing with the account. Put the local set back and repaint that
// one star, then say so in the board's own error line, which is the
// same place a failed /jobs fetch reports itself.
function revertStar(id, attempted) {
  // A token that expired mid-request signs the tab out; the local star
  // is then no less valid than any other signed-out star, so leave it.
  if (!getAuthTokens()) return;
  const s = getStarred();
  if (s.has(id) !== attempted) return; // clicked again since, and that newer intent owns the row now
  toggleStar(id);
  const now = getStarred();
  paintStar(id, now);
  if (state.starred_only) loadJobs(); // the saved view is a row off until it re-reads the set
  const errEl = document.getElementById("jobs-error");
  if (!errEl) return;
  errEl.textContent = attempted
    ? "Could not save that listing to your account. The star has been put back."
    : "Could not remove that listing from your account. The star has been put back.";
  errEl.style.display = "block";
}

// Boot merge, one direction each way. Someone who starred jobs before
// ever signing in keeps them, and someone signing in on a new phone
// gets what they starred on the laptop, which is why this is a union
// and not a download that overwrites.
async function syncSavedFromServer() {
  if (!getAuthTokens()) return;
  let data;
  try {
    data = await authedFetch("/me/saved");
  } catch {
    // Signed out, token dead, endpoint down. Local stays authoritative
    // and nothing on screen changes.
    return;
  }
  const local = getStarred();
  // /me/saved comes back newest first; reversed it appends oldest
  // first, which keeps the array's tail the recent end. Nothing reads
  // this order except the 200-id cap in renderStarredOnly.
  const remote = (data?.saved || []).map((r) => r.job_id).reverse();
  const union = new Set([...local, ...remote]);
  if (union.size !== local.size) setStarred(union);

  // Local-only ids go up so both sides end up holding the same set.
  // Not pushStar, because a failure here must not roll back a star the
  // reader set before they ever signed in. The next boot tries again.
  const remoteSet = new Set(remote);
  for (const id of local) {
    if (remoteSet.has(id)) continue;
    authedFetch(`/me/saved/${encodeURIComponent(id)}`, { method: "PUT" }).catch(() => {});
  }

  if (union.size === local.size) return;
  if (state.starred_only) {
    loadJobs(); // the saved view is now short a few rows
    return;
  }
  // Stars that arrived from another device land on rows already
  // painted, so flip those in place rather than refetching the page.
  document.querySelectorAll(".star-btn").forEach((btn) => paintStar(btn.dataset.star, union));
  if (selectedJobId) paintStar(selectedJobId, union); // open detail panel may be a job the current page does not list
}

// fetch helpers

async function getJSON(path, { signal } = {}) {
  const res = await fetch(`${API_BASE}${path}`, signal ? { signal } : undefined);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return res.json();
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === "" || v === null || v === undefined || v === false) continue;
    p.set(k, v);
  }
  return p.toString();
}

// formatting

// Google's favicon endpoint, keyed off the domain we already track for
// every job/company. Clearbit's free logo API (the previous go-to for
// this) shut down in December 2025 -- this is the zero-setup
// fallback: no signup, no key, no per-request cost. It's genuinely low
// quality though -- most sites' actual favicon.ico is a crude 16x16,
// and Google just upscales whatever's there (reported live: Overwolf's
// detailed claw-mark logo turned to mush at display size). See
// companyLogoImg below for the higher-res attempt this backs up.
function companyLogoUrl(domain, size = 32) {
  return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=${size}`;
}

// Generated, not fetched -- a flat ink-on-paper initial in the same
// square/no-radius language as everything else in DESIGN.md, for when
// no real icon worth showing exists anywhere. A data: URI, so it's
// always available with no network round-trip once picked.
function monogramLogoSvg(domain) {
  const letter = (domain || "").trim().charAt(0).toUpperCase() || "?";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#40513b"/><text x="32" y="33" font-family="Helvetica Neue, Helvetica, Arial, sans-serif" font-size="30" font-weight="700" fill="#f3f6e4" text-anchor="middle" dominant-baseline="central">${letter}</text></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

// Small, hand-verified exceptions to the guess-based cascade below, same
// shape as probe.py's own KNOWN_FALSE_POSITIVES: the general rule (try
// apple-touch-icon, then /favicon.ico, then Google) is sound, but a
// specific domain's own guessable asset can be a real, valid image file
// that just isn't a logo -- nothing in the HTTP response says so, only
// looking at the actual pixels does. duda.co/favicon.ico: confirmed
// live, a genuine 32x32 24-bit ICO, no alpha, every pixel solid white --
// stage 1/2 "succeed" (200, decodes fine) so onerror never fires and the
// cascade below never continues past it on its own. Values here are
// which stage to jump to instead of guessing that domain's own asset at
// all. Add to this as more turn up; there's no way to detect "loads
// fine but is blank" automatically without pixel access, which loading
// these cross-origin (no CORS headers on a random site's favicon.ico)
// doesn't allow.
const LOGO_STAGE_OVERRIDES = {
  "duda.co": 3, // jump straight to Google's favicon service
  // unframe.com: apple-touch-icon.png and favicon.ico both confirmed
  // dead (soft-404s), and Google's own favicon service returns a raw
  // "301 Moved" HTML error page for this domain, not an image or even
  // its usual 16x16 placeholder -- unreliable enough to skip rather
  // than keep retrying an inconsistent signal. Straight to the monogram.
  "unframe.com": 4,
  // Three where the monogram is the right answer, not a gap to fix.
  // Checked 2026-09-11: no apple-touch-icon, no favicon.ico, and
  // Google's service 404s for each. Listed so the cascade stops
  // guessing rather than issuing three doomed requests per row.
  "elbitsystems.com": 4,  // elbitsystems.com serves no icon at any of the three
  "iscar.co.il": 4,       // the site does not answer us at all
  "referralsuseonly.com": 4,  // a board placeholder, not a company with a logo
};

// Companies whose stored domain is a wrong guess from discover_companies.py's
// {token}.com heuristic (confirmed live 2026-09-08: 39 of the 160 companies
// merged that night had a company_domain that doesn't resolve at all --
// mostly enterprise ATS tenant slugs like "deloitte6" or "aecom2" that were
// never meant to be read as a domain). Fixing the icon here rather than
// renaming company_domain itself: several of these real domains (e.g.
// deloitte.com) are plausibly ALREADY a separately-tracked company under
// their own real token, so rewriting jobs.db's own domain key risks
// conflating two genuinely different job sets under one identity. This map
// only changes which domain the icon cascade below fetches from -- the
// underlying company identity, apply links, and everything else stay on the
// original (wrong) domain, same as before. See discover_companies.py's own
// verify_candidate() for the matching pipeline-side fix that stops new
// wrong guesses like these from being accepted in the first place.
const LOGO_DOMAIN_OVERRIDES = {
  // Audited 2026-09-11 across the 124 companies with open Israeli
  // listings: 14 were falling through to the monogram. Most were this
  // same shape, a tenant slug that discovery's domain guesser turned
  // into a plausible-looking domain nobody owns. Google has a good
  // 64x64 for each of the real ones.
  "sentinellabs.io": "sentinelone.com",  // SentinelOne's research blog, not the company site
  "doitintl.com": "doit.com",            // was pulling an unrelated 98x53 logo from Google
  "gongio.com": "gong.io",
  "pagayais.com": "pagaya.com",
  "eleoshealth.com": "eleos.health",
  "chainalysis-careers.com": "chainalysis.com",
  "zafran-security.com": "zafran.io",
  "chamelio.com": "chamelio.io",
  "pointfive.com": "pointfive.ai",
  "atbayjobs.com": "at-bay.com",
  "couchbaseinc.com": "couchbase.com",
  "accenturefederalservices.com": "accenturefederal.com",
  "alten-mexico-1.com": "alten.com",
  "avamere-skilled-advisors-llc.com": "avamere.com",
  "abm-careers.com": "abm.com",
  "archer56.com": "archer.com",
  "activate-interactive-pte-ltd.com": "activateinteractive.com",
  "addepar1.com": "addepar.com",
  "betatechnologiesinc.com": "beta.team",
  "asco-equipment.com": "ascoequipment.com",
  "aecom2.com": "aecom.com",
  "aboutyougmbh.com": "aboutyou.de",
  "americanironandmetal.com": "aimetals.com",
  "apf-entreprises.com": "apf-entreprises.fr",
  "applusidiada1.com": "applusidiada.com",
  "artemedse.com": "artemed.de",
  "asburycommunities.com": "asbury.org",
  "avaloq1.com": "avaloq.com",
  "baywaag.com": "baywa.com",
  "bertelsmann-jobs.com": "bertelsmann.com",
  "beumergroup1.com": "beumergroup.com",
  "cityandcountyofsanfrancisco1.com": "sf.gov",
  "collabera2.com": "collabera.com",
  "colliers1.com": "colliers.com",
  "colliersinternationalemea.com": "colliers.com",
  "contilia1.com": "contilia.com",
  "culinagroup1.com": "culinagroup.com",
  "deloitteat.com": "deloitte.com",
  "deloittenordic.com": "deloitte.com",
  "deloitte6.com": "deloitte.com",
  "deutschetelekomitsolutionsslovakia.com": "t-systems.com",
  "deutschetelekomitsolutions.com": "t-systems.com",
  // tenableinc.com's own apple-touch-icon.png is a soft-404 (HTTP 200,
  // Content-Type: text/html, not an image) -- reported live, and it's
  // the wrong domain anyway. discover_companies.py's own _guess_domain
  // now strips this exact "inc" shape going forward (see its docstring).
  "tenableinc.com": "tenable.com",
  // wix2.com / redwoodmaterials.co: both resolve to 200 OK, but both
  // are domain-parking pages (confirmed live: identical IP, identical
  // 114-byte "window.location.href" redirect stub) -- a HEAD-only check
  // can't tell that from a real site. discover_companies.py's own
  // _guess_domain now inspects response bodies for exactly this going
  // forward (see _looks_parked).
  "wix2.com": "wix.com",
  "redwoodmaterials.co": "redwoodmaterials.com",
  // reindeer-ai.com doesn't exist at all (confirmed: DNS NXDOMAIN) --
  // the real domain is reindeer.ai, the token's own trailing "-ai"
  // standing in for the TLD dot, not part of the name. Same pattern
  // now tried automatically going forward (see _HYPHEN_TLD_RE).
  "reindeer-ai.com": "reindeer.ai",
  // cermaticom.com: no real domain found -- keeps the monogram fallback.
};

// Reported live: Overwolf's real favicon.ico is a genuine 16x16 (verified
// via a direct curl, not assumed) with no apple-touch-icon anywhere on
// the site either -- Google's service was never the bug there, it was
// already showing the best real source, just upscaled. Four tiers, each
// one a same-origin/no-key image request or a local fallback, cheapest
// and most honest first:
//   1. apple-touch-icon.png -- the high-res convention, when it exists.
//   2. the domain's own favicon.ico, fetched directly (not through a
//      resizing proxy).
//   3. Google's favicon service -- catches the case a site's icon isn't
//      at a guessable path at all (declared via a <link> tag instead);
//      that's the one failure mode a direct path guess can't solve.
//   4. the generated monogram, unconditionally available.
// Stage 2 used to reject anything <=32px naturalWidth and drop straight
// to the monogram rather than show it upscaled -- reported live, that
// meant real, recognizable marks (Cisco, Mastercard: both genuinely just
// a small classic .ico, confirmed live) were losing to a flat letter
// square. A soft, real mark beats a generic initial for a company this
// recognizable, so stage 2 now renders whatever it gets, same as stage 3
// always has.
// Once ONE <img> for a domain settles on a final stage (a real icon
// found, or every stage exhausted down to the monogram), remember it --
// so the next occurrence of the same domain skips straight to the
// known-good stage instead of re-running the whole apple-touch-icon ->
// favicon.ico -> Google fallback from scratch. Reported live: a
// company-filtered search with 50 rows of the same domain made up to
// 150 redundant network requests (a failed stage's own 404 isn't
// cached by the browser by default), serialized by the browser's
// per-host connection limit -- visibly slow for exactly that shape of
// page, and for the Fastest Growing Companies panel + that same
// company's own rows both resolving the same domain independently.
// Stores only the STAGE, not a fully-built src -- callers ask for
// different sizes (the Top Hiring panel uses 16px, job rows something
// larger), and a cached src would freeze whichever size resolved it
// first. Persisted to localStorage too, not just this page's in-memory
// session, so a repeat VISIT benefits, not just repeated occurrences on
// one page -- wrapped in try/catch since localStorage can throw
// (private browsing, blocked site data), same as every other
// try/catch around it in this file.
// Bumped to v2 on 2026-09-11 with the override additions above. The
// cached value short-circuits the cascade, so a visitor who already
// resolved sentinellabs.io to the monogram would keep seeing it forever
// no matter what the override table says. Any future change to
// LOGO_DOMAIN_OVERRIDES or LOGO_STAGE_OVERRIDES has to bump this too.
const LOGO_RESOLVED_KEY = "iljobs-logo-resolved-v2";
let logoResolvedCache = new Map();
try {
  logoResolvedCache = new Map(Object.entries(JSON.parse(localStorage.getItem(LOGO_RESOLVED_KEY) || "{}")));
} catch { /* private browsing or blocked storage -- fall back to in-memory only */ }

function rememberLogoStage(domain, stage) {
  logoResolvedCache.set(domain, stage);
  try {
    localStorage.setItem(LOGO_RESOLVED_KEY, JSON.stringify(Object.fromEntries(logoResolvedCache)));
  } catch { /* same as above */ }
}
window.rememberLogoStage = rememberLogoStage;

// A logo the server already resolved and verified (company_logo.py: the
// company's own upload to its ATS, else whatever its site declares, else
// Google's favicon service). Passed straight through, because it was
// fetched and checked once rather than guessed here on every page view.
// The cascade below remains for rows loaded before the column existed,
// and for the panels that have a domain but no job record to read from.
function companyLogoImg(domain, size, extraClass = "", resolved = null) {
  if (resolved) {
    const cls = extraClass ? `company-logo ${extraClass}` : "company-logo";
    // Two fallbacks, not one. A resolved URL can fail for reasons the
    // server cannot see: a CDN 404s a logo that verified weeks ago, and
    // an ad blocker refuses anything served from an ad network's own
    // domain. Reported live: Taboola's logo is a valid 300x300 PNG on
    // taboola.com, which Brave blocks outright, so a company that used
    // to show an icon started showing a letter. Google's favicon service
    // is a neutral host and survives both, so it goes between the
    // resolved URL and giving up.
    const logoDomain = LOGO_DOMAIN_OVERRIDES[domain] || domain;
    return `<img class="${cls}" src="${escapeHtml(resolved)}" alt="" loading="lazy"
      data-google="${escapeHtml(companyLogoUrl(logoDomain, size))}"
      data-monogram="${escapeHtml(monogramLogoSvg(domain))}"
      onerror="if(this.dataset.google&&this.src!==this.dataset.google){this.src=this.dataset.google;}else{this.onerror=null;this.src=this.dataset.monogram;}" />`;
  }
  return companyLogoGuess(domain, size, extraClass);
}

function companyLogoGuess(domain, size, extraClass = "") {
  // LOGO_DOMAIN_OVERRIDES only affects where the icon itself is fetched
  // from -- domain (used below for the monogram initial, and by every
  // caller for the actual company identity/apply link) stays as-is.
  const logoDomain = LOGO_DOMAIN_OVERRIDES[domain] || domain;
  const touchIcon = escapeHtml(`https://${logoDomain}/apple-touch-icon.png`);
  const directFavicon = escapeHtml(`https://${logoDomain}/favicon.ico`);
  const googleFavicon = escapeHtml(companyLogoUrl(logoDomain, size));
  const monogram = escapeHtml(monogramLogoSvg(domain));
  const cls = extraClass ? `company-logo ${extraClass}` : "company-logo";
  const startStage = logoResolvedCache.get(domain) || LOGO_STAGE_OVERRIDES[logoDomain] || 1;
  const startSrc = { 1: touchIcon, 2: directFavicon, 3: googleFavicon, 4: monogram }[startStage];
  // Reported live (tomorrow.io): neither its favicon.ico nor its
  // apple-touch-icon exist, and Google's own favicon service can't find
  // one for it either -- but Google still answers 200 with an image
  // (its own generic default-globe placeholder), not a load failure, so
  // onerror alone never advances past it to the monogram. Confirmed
  // live: that generic placeholder always comes back a fixed 16x16, at
  // ANY requested sz -- but so can a real, found icon (reported live:
  // salesforce.com's own real favicon is genuinely only cached at 32x32
  // on Google's end even at sz=64, and a first version of this check
  // treated "smaller than requested" as "Google has nothing," wrongly
  // sending Salesforce's real icon to the monogram too). 16x16 exactly
  // is Google's own specific "nothing found" size -- but ONLY a
  // meaningful signal when we actually asked for more than that: the
  // Companies with most open roles panel calls this with size=16 itself (a real,
  // correctly-found 16x16 icon there is indistinguishable from the
  // placeholder by dimensions alone), and a second version of this
  // check missed that, wrongly monogram-ing real icons for every
  // company on that panel. Gated on size > 16 now, not just the pixel
  // dimensions matching.
  // rememberLogoStage calls below are what make logoResolvedCache above
  // actually useful: the FIRST <img> for a domain to settle (success or
  // exhausted to the monogram) is what every later occurrence on this
  // page, or a future visit, gets to skip straight to.
  return `<img class="${cls}" src="${startSrc}" alt="" loading="lazy"
    data-domain="${escapeHtml(domain)}" data-stage="${startStage}" data-size="${size}"
    data-direct-favicon="${directFavicon}" data-google-favicon="${googleFavicon}" data-monogram="${monogram}"
    onerror="if(this.dataset.stage==='1'){this.dataset.stage='2';this.src=this.dataset.directFavicon;}else if(this.dataset.stage==='2'){this.dataset.stage='3';this.src=this.dataset.googleFavicon;}else{this.onerror=null;this.onload=null;this.src=this.dataset.monogram;rememberLogoStage(this.dataset.domain,4);}"
    onload="if(this.dataset.stage==='3'&&Number(this.dataset.size)>16&&this.naturalWidth===16&&this.naturalHeight===16){this.onerror=null;this.onload=null;this.src=this.dataset.monogram;rememberLogoStage(this.dataset.domain,4);}else{rememberLogoStage(this.dataset.domain,Number(this.dataset.stage));}" />`;
}

function fmtInt(n) {
  return (n ?? 0).toLocaleString("en-US");
}

function fmtPct(n) {
  return `${Math.round((n ?? 0) * 1000) / 10}%`;
}

function fmtAge(days) {
  if (days === null || days === undefined) return "-";
  const totalMinutes = Math.floor(days * 24 * 60);
  const totalHours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  const hours = totalHours % 24;

  if (totalMinutes < 1) return "<1M";
  if (totalHours < 1) return `${minutes}M`;
  if (totalHours < 12) return `${totalHours}H ${minutes}M`;
  if (totalHours < 24) return `${totalHours}H`;
  if (days < 3) return `${Math.floor(days)}D ${hours}H`;
  return `${Math.floor(days)}D`;
}

function fmtMinutesAgo(mins) {
  if (mins === null || mins === undefined) return "UNKNOWN";
  if (mins < 60) return `${Math.round(mins)}M AGO`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}H AGO`;
  return `${Math.round(mins / (60 * 24))}D AGO`;
}

// Deliberately no "AGO". This is the age of the data being shown, not
// the time since some event, and "29M AGO" under a heading about the
// API invited exactly the wrong reading.
// Spelled out rather than 18M/3H/2D. Next to a heading about how old the
// data is, "18M" reads as eighteen months at least as easily as eighteen
// minutes, and the two answers are a year apart.
function fmtDataAge(mins) {
  if (mins === null || mins === undefined) return null;
  if (mins < 60) return `${Math.round(mins)} min`;
  if (mins < 60 * 24) {
    const h = Math.round(mins / 60);
    return `${h} hour${h === 1 ? "" : "s"}`;
  }
  const d = Math.round(mins / (60 * 24));
  return `${d} day${d === 1 ? "" : "s"}`;
}

function escapeHtml(s) {
  return (s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// metrics dashboard

// Set from both renderMetrics() (every 2-min /stats poll) and
// refreshFreshness() (every 30s /health poll, see below), read by
// tickApiStatus() so the "X AGO" text and the sync countdown can keep
// moving client-side between polls instead of sitting frozen at
// whatever the last poll said.
let lastCheckedAt = null;

let lastReadingAt = null;

// last_checked only ever moves forward (MAX(companies.last_checked) in
// the DB), so two independently-cached samples of it (/stats and
// /health are separate CloudFront cache entries, each up to 120s stale
// on its own clock -- see infra/cloudfront.tf's api cache policy) should
// never be combined by just overwriting: whichever poll happens to land
// on a staler cached copy would yank the countdown backwards. Take the
// newer of the two instead.
function setLastCheckedAt(iso) {
  if (!iso) return;
  // When we last actually heard back, as opposed to how old the data was
  // when we heard. Set before the monotonic guard below, because a poll
  // that returns the same timestamp still proves the API answered.
  lastReadingAt = Date.now();
  const t = new Date(iso).getTime();
  if (lastCheckedAt === null || t > lastCheckedAt) lastCheckedAt = t;
}



// Shared by the topbar status dot/text and the API Status card: past
// this, both flip from LIVE (green) to OFFLINE (red) together.
//
// This tracked the write cycle up to 75 while the merge was hourly. It
// has to come down with it, or the site claims LIVE through fifteen
// consecutive missed cycles, which is most of a morning of stale data
// wearing a green light.
//
// 20 is four cycles rather than the 1.25x the hourly threshold used,
// because at five minutes a single slow cycle is a much larger share of
// the budget and the countdown poll itself is 2 minutes wide. Anything
// tighter flaps on ordinary jitter.
const FRESH_THRESHOLD_MINUTES = 20;

// The number here is MAX(companies.last_checked) from the current
// snapshot, so it answers "how old is the freshest listing data I'm
// holding", not "when did I last reach the API" and not "when did the
// sync run". Those are three different clocks and the card used to
// present the first as if it were the second: a heading of "API Status"
// over "29M AGO" reads as the API having been unreachable for half an
// hour, which is alarming and wrong, since the API answers in
// milliseconds and the figure is really bounded by the write cycle.
// Reported live, asking whether the sync cadence was what the card
// implied. It was not, and it has been wrong in this spot twice since.
// Three states on AWS's service-health vocabulary, not two.
//
// A binary green/red had nothing to say about the middle, which is where
// the interesting failures live: the snapshot is written every 5
// minutes, so one 12 minutes old has quietly missed two cycles while
// still sitting inside the 20-minute threshold that decides whether the
// board claims to be current at all. That is worth showing as degraded
// rather than as either fine or broken.
//
// DEGRADED_AFTER_MINUTES is deliberately two missed cycles rather than
// one. A single late merge is ordinary.
const DEGRADED_AFTER_MINUTES = 10;

// Old data with a live pipeline behind it is not an outage. Reported
// live: the tile said OFFLINE at 21 minutes while the merge had reported
// in two minutes earlier and was batching the next update. That scares a
// visitor over nothing.
//
// The merge writes its status on every 5-minute run, idle or not, and the
// API already marks a merge that died mid-run as "unknown" after 15
// minutes. So a merge status younger than PIPELINE_HEARTBEAT_MINUTES that
// is not an error means the pipeline is alive, and the status shows
// UPDATING with a spinner instead of DEGRADED or OFFLINE.
//
// UPDATING_CEILING_MINUTES keeps that honest. A merge that keeps running
// without ever applying anything is still a broken pipeline, and past 45
// minutes of old data the status says OFFLINE whatever the heartbeat says.
const PIPELINE_HEARTBEAT_MINUTES = 10;
const UPDATING_CEILING_MINUTES = 45;

function pipelineAlive() {
  const merge = pipelinePhase && pipelinePhase.merge;
  if (!merge || !merge.at || ["error", "unknown"].includes(merge.phase)) return false;
  return (Date.now() - new Date(merge.at).getTime()) / 60000 <= PIPELINE_HEARTBEAT_MINUTES;
}

// DEGRADED is eight characters and the tile is narrow, so the type is
// sized to the longest word rather than the words cut to the type (see
// #metric-api-status .value). It used to render as DEGRADE, clipped
// mid-word by the panel's overflow, and OFFLINE was quietly overflowing
// the same tile without being obvious enough for anyone to catch.
const STATUS_LEVELS = {
  operational: { symbol: "status-positive", label: "Operational", text: "LIVE" },
  degraded: { symbol: "status-warning", label: "Degraded", text: "DEGRADED" },
  outage: { symbol: "status-negative", label: "No recent updates", text: "OFFLINE" },
  updating: { symbol: "status-updating", label: "Updating", text: "UPDATING" },
};

function apiStatusFields() {
  // The age is only meaningful if our own reading is current. Both polls
  // are gated on the tab being visible, and a sleeping machine misses
  // them with the tab still in front, while the one-second tick counts up
  // regardless. So an idle tab walks itself to OFFLINE while every cycle
  // behind it ran on time: CloudWatch shows both halves of the pipeline
  // firing every 5 minutes, 48 for 48, through the window a tile claimed
  // 20 minutes of silence.
  //
  // Freeze the clock at the last reading we actually took rather than
  // counting past it. Staleness we have not observed is not evidence.
  // The next successful poll unfreezes it.
  const stale = lastReadingAt === null || Date.now() - lastReadingAt > HEALTH_POLL_MS * 2;
  const readAt = stale ? lastReadingAt : Date.now();
  const minutesSince = lastCheckedAt === null || readAt === null ? null : (readAt - lastCheckedAt) / 60000;
  const age = minutesSince ?? 9999;
  const fresh = age <= FRESH_THRESHOLD_MINUTES;
  let level = !fresh ? "outage" : age > DEGRADED_AFTER_MINUTES ? "degraded" : "operational";
  if (level !== "operational" && age <= UPDATING_CEILING_MINUTES && pipelineAlive()) level = "updating";
  const spelled = fmtDataAge(minutesSince);
  return {
    fresh,
    level,
    // The word follows the level, not `fresh`. It used to follow `fresh`,
    // so the whole degraded band rendered a green LIVE beside the orange
    // warning glyph: at 18 minutes the tile said everything was fine and
    // flagged a problem in the same line. Reported live, from a
    // screenshot of exactly that.
    value: STATUS_LEVELS[level].text,
    sub: spelled === null ? "age unknown" : `${spelled} old`,
  };
}

// One writer for both places the state appears, so the topbar and the
// Data Health tile cannot drift apart. Swapping the <use> target rather
// than the markup keeps the sprite as the single definition of each
// glyph.
function paintStatusIcon(el, level) {
  if (!el) return;
  const spec = STATUS_LEVELS[level] || STATUS_LEVELS.operational;
  const use = el.querySelector("use");
  if (use) use.setAttribute("href", `#${spec.symbol}`);
  el.classList.remove("degraded", "outage", "updating");
  if (level !== "operational") el.classList.add(level);
  el.setAttribute("aria-label", spec.label);
}

function statusIconHtml(level) {
  const spec = STATUS_LEVELS[level] || STATUS_LEVELS.operational;
  const cls = level === "operational" ? "status-icon" : `status-icon ${level}`;
  return `<svg class="${cls}" role="img" aria-label="${spec.label}">`
    + `<use href="#${spec.symbol}"></use></svg>`;
}

// What the pipeline is doing, in its own words.
//
// Replaced a countdown to the next sync. That was always an estimate of
// a routine cycle rather than an observation, and an estimate of
// something that already happened five minutes ago is not information.
// Every stage of the pipeline already writes what it is doing to S3
// (see _write_status in the three handlers), so this shows that instead.
//
// Both sides, not just the merge. The old version deliberately ignored
// the scrape side on the grounds that this card is about data freshness
// and only the merge moves it. That was the right call for a countdown
// and the wrong one here: "sweeping 1,100 of 3,434 companies due now" is
// exactly the kind of thing worth seeing, and it is true far more of the
// time than the merge is busy.
//
// The merge wins a tie because it is the stage that changes what a
// visitor is looking at.
function pipelineActivityText() {
  if (!pipelinePhase) return null;
  const quiet = (p) => !p || !p.phase || ["idle", "unknown"].includes(p.phase);
  const describe = (p) => (p.detail ? `${p.phase}: ${p.detail}` : p.phase);

  for (const side of [pipelinePhase.merge, pipelinePhase.scrape]) {
    if (!quiet(side)) return describe(side);
  }
  // Nothing running. The idle detail says what the last run actually
  // did, which beats inventing a prediction about the next one.
  const last = pipelinePhase.merge || pipelinePhase.scrape;
  return last && last.detail ? last.detail : null;
}

function tickApiStatus() {
  const card = document.getElementById("metric-api-status");
  if (!card) return;
  const { level, value, sub } = apiStatusFields();
  // Green is for operational only. Degraded and outage colour the word
  // to match their own glyph.
  card.classList.toggle("highlight", level === "operational");
  card.classList.toggle("degraded", level === "degraded");
  card.classList.toggle("outage", level === "outage");
  card.classList.toggle("updating", level === "updating");
  // Only when it changes. This runs every second, and rewriting the markup
  // each time recreates the glyph, which restarted the Updating spinner's
  // rotation every second and made it stutter.
  const valueEl = card.querySelector(".value");
  const valueHtml = `${statusIconHtml(level)}${escapeHtml(value)}`;
  if (valueEl.dataset.painted !== valueHtml) {
    valueEl.innerHTML = valueHtml;
    valueEl.dataset.painted = valueHtml;
  }
  card.querySelector(".sub").textContent = sub;
  const syncEl = card.querySelector(".sync-countdown");
  if (syncEl) syncEl.textContent = pipelineActivityText() ?? "";
  paintStatusIcon(document.getElementById("status-dot"), level);
  document.getElementById("status-text").textContent = STATUS_LEVELS[level].text;
}

// The scoped answer the API gave for one exact set of board filters,
// plus the filter string it was asked for. The key is what lets the
// 2-minute global /stats tick re-render these tiles without quietly
// repainting a filtered board with whole-board numbers: if the key does
// not match what the filter bar says right now, the numbers in here
// belong to a search nobody is looking at.
// `data: null` with `degraded: true` is the answer that came back
// without a `scoped` object at all.
let latestScoped = null;

// One shape for the scopeable numbers, whichever half of the payload
// they came out of. /api/stats' `scoped` object mirrors totals.* /
// throughput.* / age.* / top_companies field for field on purpose, so
// the tiles never have to branch on where a number came from.
function scopeShape(src) {
  return {
    open_jobs: src.open_jobs,
    companies_hiring: src.companies_hiring,
    new_jobs_24h: src.new_jobs_24h,
    new_jobs_7d: src.new_jobs_7d,
    closed_jobs_24h: src.closed_jobs_24h,
    closed_jobs_7d: src.closed_jobs_7d,
    median_open_days: src.median_open_days,
    oldest_open_days: src.oldest_open_days,
    top_companies: Array.isArray(src.top_companies) ? src.top_companies : [],
  };
}

function globalScope(stats) {
  return scopeShape({
    ...stats.totals,
    ...stats.throughput,
    ...stats.age,
    top_companies: stats.top_companies || [],
  });
}

// Four states, because three of them look identical if you only track
// "filtered or not" and each one has to be labelled differently:
//
//   global       nothing real is filtered, the tiles are whole-board
//   pending      filtered, the scoped answer has not landed yet
//   scoped       filtered, the numbers on screen are the filtered ones
//   unavailable  filtered, but the API answered without a `scoped` key
//                (a stale artifact, or the route before it shipped), so
//                the numbers fall back to whole-board and say so
//
// The "is anything really filtered" test is refreshFacetOptions', not a
// second definition of its own: the board sends a confidence on every
// request, so it never counts as a filter here.
function currentScopeMode() {
  const params = qs({ ...currentFilterParams(), confidence: "" });
  if (!params) return "global";
  if (!latestScoped || latestScoped.params !== params) return "pending";
  return latestScoped.degraded ? "unavailable" : "scoped";
}

// What the board is filtered by, in the words the filter bar itself
// uses. Read from state rather than from the response, because it has
// to be right the moment a filter changes, which is up to 2.9s before
// the numbers it describes arrive.
// starred_only and sort/offset are deliberately absent: the first is a
// client-local view the API never sees, the other two do not change
// which listings are counted.
function activeFilterSummary() {
  const parts = [];
  if (state.search) parts.push(`"${state.search}"`);
  if (state.department.length) parts.push(state.department.join(", "));
  if (state.seniority.length) parts.push(state.seniority.map((s) => SENIORITY_LABELS[s] || s).join(", "));
  if (state.company.length) parts.push(state.company.join(", "));
  if (state.country.length) parts.push(state.country.map(countryLabel).join(", "));
  if (state.city.length) parts.push(state.city.join(", "));
  if (state.workplace.length) parts.push(state.workplace.map((w) => WORKPLACE_LABELS[w] || w).join(", "));
  if (state.skills.length) parts.push(`${state.skills.length} CV skill${state.skills.length === 1 ? "" : "s"}`);
  if (state.max_age_days) parts.push(`posted in the last ${state.max_age_days} days`);
  return parts;
}

// Says so plainly when nothing is applied rather than disappearing. An
// indicator that is only there sometimes teaches the reader nothing
// about the times it is missing, and "these are global right now" is
// exactly what they need to know to trust the tile above.
function renderScopeLine() {
  const el = document.getElementById("stats-scope");
  if (!el) return;
  const mode = currentScopeMode();
  const applied = activeFilterSummary().join(" · ");
  if (mode === "global") {
    el.textContent = "No filters applied, so these are whole-board totals.";
    return;
  }
  if (mode === "pending") {
    el.textContent = `${applied}. Counting…`;
    return;
  }
  if (mode === "unavailable") {
    el.textContent = `${applied}. No scoped totals came back, so these are whole-board numbers.`;
    return;
  }
  el.textContent = applied;
}

function renderMetrics(stats) {
  setLastCheckedAt(stats.freshness.last_checked);
  renderScopedMetrics(stats);
  renderPipelineTile();
}

// The five tiles that can follow the board's filters. Data Health is not
// among them any more; it answers a question about the pipeline, not
// about the selected listings, and lives in its own block below.
function renderScopedMetrics(stats) {
  const el = document.getElementById("metrics-grid");
  const mode = currentScopeMode();
  const pending = mode === "pending";
  // Only "scoped" puts filtered numbers on screen. "pending" is about to,
  // so it labels itself the same way; "unavailable" falls back to
  // whole-board numbers and has to keep the whole-board labels with them.
  const narrowed = mode === "scoped" || pending;
  const d = mode === "scoped" ? latestScoped.data : globalScope(stats);
  renderScopeLine();

  const cards = [
    {
      // The word "global" survives on this tile only while the number
      // under it really is global. A filtered count beneath a label
      // reading GLOBAL OPEN JOBS is the failure this whole section
      // exists to rule out.
      label: narrowed ? "Open Roles" : "Global Open Jobs",
      value: fmtInt(d.open_jobs),
      // open_jobs_best_effort is a whole-board figure with no scoped
      // twin in the contract, so it cannot ride along under a filtered
      // count pretending to describe it.
      sub: narrowed ? "matching these filters" : `${fmtInt(stats.meta.open_jobs_best_effort)} more unverified`,
      hl: true,
    },
    {
      label: "Companies Hiring",
      value: fmtInt(d.companies_hiring),
      sub: narrowed ? "with a matching open role" : "with a fresh open role",
    },
    {
      // The label said 7d while the number under it was the 24h figure,
      // with the real 7d total demoted to the caption. Both tiles read
      // the same way, so both said the wrong period. Caught while
      // relabelling the panel for scoping, which is the whole point of
      // that exercise: a number nobody can name is worse than no number.
      label: "New Listings in 24h",
      value: `+${fmtInt(d.new_jobs_24h)}`,
      sub: `${fmtInt(d.new_jobs_7d)} in 7d`,
      hl: d.new_jobs_24h > 0,
    },
    {
      label: "Closed in 24h",
      value: `-${fmtInt(d.closed_jobs_24h)}`,
      sub: `${fmtInt(d.closed_jobs_7d)} in 7d`,
    },
    {
      label: "Median Open Age",
      value: fmtAge(d.median_open_days),
      sub: `oldest ${fmtAge(d.oldest_open_days)}`,
    },
  ];

  el.innerHTML = cards
    .map(
      (c) => `
      <div class="metric-card ${pending ? "pending" : c.hl ? "highlight" : ""}"${pending ? ' aria-busy="true"' : ""}>
        <div class="label">${c.label}</div>
        <div>
          <div class="value">${pending ? '<span class="skeleton" aria-hidden="true"></span>' : c.value}</div>
          <div class="sub">${pending ? "" : c.sub}</div>
        </div>
      </div>`
    )
    .join("");
}

// Never scoped, and its own block says why. It describes the freshness
// of the whole scrape, so narrowing it to a filter would be meaningless
// even if the API offered a way to.
function renderPipelineTile() {
  const el = document.getElementById("pipeline-grid");
  const status = apiStatusFields();
  const sub2 = pipelineActivityText();
  // The first paint has to land on the same class the one-second tick
  // would set right after, or the tile flashes green before correcting.
  el.innerHTML = `
      <div class="metric-card ${status.level === "operational" ? "highlight" : status.level}" id="metric-api-status"
           title="Freshness of the whole pipeline across every company we poll. Never narrowed by the board's filters.">
        <div class="label">Data Health</div>
        <div>
          <div class="value">${statusIconHtml(status.level)}${escapeHtml(status.value)}</div>
          <div class="sub">${status.sub}</div>
          ${sub2 ? `<div class="sync-countdown">${sub2}</div>` : ""}
        </div>
      </div>`;

  paintStatusIcon(document.getElementById("status-dot"), status.level);
  document.getElementById("status-text").textContent = STATUS_LEVELS[status.level].text;
}

// Recomputes from lastCheckedAt every 1s -- the sync countdown needs a
// real per-second tick to read as "live"; the AGO text along for the
// ride is a no-op most seconds, negligible cost either way.
const API_STATUS_TICK_MS = 1_000;

// Market-insight panels: who's hiring, what for, where. No ATS-vendor
// breakdown here; that's plumbing, not a market signal (still available
// as open_jobs_by_ats for anyone polling the raw API).
function renderBarList(rows, nameKey, { clickable = false, limit = 0 } = {}) {
  const max = Math.max(1, ...rows.map((r) => r.n));
  return rows
    .map((r, i) => {
      const name = escapeHtml(r[nameKey]);
      // Rows past `limit` are rendered but hidden until the panel is
      // expanded (see .bar-row-extra). title= shows a truncated name in full.
      const extra = limit && i >= limit ? " bar-row-extra" : "";
      return `
      <div class="bar-row${clickable ? " clickable" : ""}${extra}" ${clickable ? `data-company="${name}"` : ""}>
        <div class="name" title="${name}">${clickable ? companyLogoImg(r[nameKey], 16, "", r.logo_url || null) : ""}${name}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${(r.n / max) * 100}%"></div></div>
        <div class="n">${fmtInt(r.n)}</div>
      </div>`;
    })
    .join("");
}

// SVG bar chart, 14 days of new-listing counts, with a closed-jobs line
// on its own scale. Closed counts run much smaller, so sharing one
// scale would flatline the line near zero. Not pixel-comparable to each
// other; the legend says so. viewBox-scaled for responsiveness; native
// <title> gives a free per-point tooltip.
// Smooth SVG path through a list of [x,y] points via Catmull-Rom-to-Bezier
// conversion -- no charting library, just the standard spline formula.
// Centripetal parameterization (alpha=0.5): the variant that stays
// well-behaved (no loops/cusps) even when points aren't evenly spaced,
// unlike the uniform (alpha=0) version. Barry & Goldman's formula, via
// https://qroph.github.io/2018/07/30/smooth-paths-using-catmull-rom-splines.html
// Clamps the neighbor lookup at both ends so the curve doesn't overshoot
// past the first/last point.
function smoothPath(points, alpha = 0.5) {
  if (points.length < 2) return "";
  const p = points;
  const n = p.length;
  const dist = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1]);
  let d = `M${p[0][0].toFixed(1)},${p[0][1].toFixed(1)}`;

  for (let i = 0; i < n - 1; i++) {
    const p0 = p[Math.max(i - 1, 0)];
    const p1 = p[i];
    const p2 = p[i + 1];
    const p3 = p[Math.min(i + 2, n - 1)];

    const t01 = Math.pow(dist(p0, p1), alpha) || 1e-6;
    const t12 = Math.pow(dist(p1, p2), alpha) || 1e-6;
    const t23 = Math.pow(dist(p2, p3), alpha) || 1e-6;

    const m1x = p2[0] - p1[0] + t12 * ((p1[0] - p0[0]) / t01 - (p2[0] - p0[0]) / (t01 + t12));
    const m1y = p2[1] - p1[1] + t12 * ((p1[1] - p0[1]) / t01 - (p2[1] - p0[1]) / (t01 + t12));
    const m2x = p2[0] - p1[0] + t12 * ((p3[0] - p2[0]) / t23 - (p3[0] - p1[0]) / (t12 + t23));
    const m2y = p2[1] - p1[1] + t12 * ((p3[1] - p2[1]) / t23 - (p3[1] - p1[1]) / (t12 + t23));

    // Hermite tangents -> cubic Bezier control points.
    const c1x = p1[0] + m1x / 3;
    const c1y = p1[1] + m1y / 3;
    const c2x = p2[0] - m2x / 3;
    const c2y = p2[1] - m2y / 3;

    d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}

function renderTrendChart(daily) {
  // Each line is normalized to its own max, not a shared one: closed
  // counts run much smaller than new counts, so sharing a scale would
  // flatline it near zero.
  const maxNew = Math.max(1, ...daily.map((d) => d.n));
  const maxClosed = Math.max(1, ...daily.map((d) => d.closed || 0));
  const w = 320;
  const h = 64;
  const stepX = daily.length > 1 ? w / (daily.length - 1) : 0;

  const newXY = daily.map((d, i) => [i * stepX, h - (d.n / maxNew) * h]);
  const closedXY = daily.map((d, i) => [i * stepX, h - ((d.closed || 0) / maxClosed) * h]);
  const newLine = smoothPath(newXY);
  const closedLine = smoothPath(closedXY);

  return `
    <svg class="trend-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path class="trend-line" d="${newLine}"><title>New listings</title></path>
      <path class="trend-line closed" d="${closedLine}"><title>Closed listings</title></path>
    </svg>
    <div class="trend-legend">
      <span><span class="dot new"></span>New</span>
      <span><span class="dot closed"></span>Closed</span>
    </div>
    <div class="trend-axis"><span>${daily[0].date.slice(5)}</span><span>${daily[daily.length - 1].date.slice(5)}</span></div>`;
}

// Standalone line chart: open_jobs_history, reconstructed from
// first_seen/closed_at rather than a real snapshot.
function renderOpenJobsChart(history) {
  const max = Math.max(1, ...history.map((d) => d.n));
  const w = 320;
  const h = 64;
  const stepX = history.length > 1 ? w / (history.length - 1) : 0;
  const xy = history.map((d, i) => [i * stepX, h - (d.n / max) * h]);
  const line = smoothPath(xy);
  return `
    <svg class="trend-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path class="trend-line open" d="${line}"><title>Open jobs over time</title></path>
    </svg>
    <div class="trend-axis"><span>${history[0].date.slice(5)}</span><span>${history[history.length - 1].date.slice(5)}</span></div>`;
}

// One headline percentage: how much of the open board looks like it's
// stopped moving. threshold_days comes from the API, not hardcoded here,
// so a backend change to the cutoff doesn't need a matching frontend edit.
function renderGhostStat(ghost, openJobs) {
  const pct = Math.round(ghost.dormant_pct * 1000) / 10;
  return `
    <div class="ghost-pct">${pct}%</div>
    <div class="ghost-sub">${fmtInt(ghost.dormant_count)} of ${fmtInt(ghost.sample_size)} open listings haven't been filled in over ${ghost.threshold_days} days (${fmtInt(openJobs)} open in total).</div>`;
}

// Every panel in the Market overview block is whole-board.
function renderPanels(stats) {
  const el = document.getElementById("panel-grid");

  el.innerHTML = `
    <div class="panel">
      <div class="panel-title">New Listings, Last 14 Days</div>
      ${renderTrendChart(stats.daily_new_jobs)}
    </div>
    <div class="panel">
      <div class="panel-title">Open Jobs Over Time</div>
      ${renderOpenJobsChart(stats.open_jobs_history)}
    </div>
    <div class="panel">
      <div class="panel-title">Fastest Growing (New Reqs, 7D)</div>
      ${
        stats.top_movers_7d.length
          ? renderBarList(stats.top_movers_7d, "domain", { clickable: true })
          : '<div class="sub" style="color:var(--grey)">Nothing new in the last 7 days.</div>'
      }
    </div>
    <div class="panel">
      <div class="panel-title">Top Categories</div>
      ${renderBarList(stats.top_departments, "department")}
    </div>
    <div class="panel">
      <div class="panel-title">Seniority Spread</div>
      ${renderBarList(
        stats.seniority_breakdown.map((r) => ({ seniority: SENIORITY_LABELS[r.seniority] || r.seniority, n: r.n })),
        "seniority"
      )}
    </div>
    <div class="panel">
      <div class="panel-title">Dormant Listings</div>
      ${renderGhostStat(stats.ghost, stats.totals.open_jobs)}
    </div>`;

  wireCompanyBarClicks(el);
}

// How many companies show before View all. The API returns 10; at the
// sidebar's width ten long domains read as a wall. Module-level so the
// 2-minute stats refresh doesn't collapse a list the reader opened.
const COMPANY_ROWS_SHOWN = 7;
let companiesExpanded = false;

// Companies with most open roles, the one bar list the contract can
// narrow, so it sits in the Current search block with the tiles rather
// than beside the charts.
function renderScopedPanels(stats) {
  const el = document.getElementById("scoped-panel-grid");
  const mode = currentScopeMode();

  if (mode === "pending") {
    // Bones rather than the previous filter's leaderboard. A list of
    // companies is read as an answer, and holding the old one there for
    // up to 2.9s answers a question the reader has stopped asking.
    el.innerHTML = `
      <div class="panel sk-panel" aria-busy="true">
        <span class="skeleton sk-label"></span>
        <span class="skeleton sk-bar"></span>
        <span class="skeleton sk-bar"></span>
        <span class="skeleton sk-bar"></span>
        <span class="skeleton sk-bar"></span>
      </div>`;
    return;
  }

  const scoped = mode === "scoped";
  const rows = scoped ? latestScoped.data.top_companies : stats.top_companies || [];
  const moreLabel = () => (companiesExpanded ? "Show fewer" : `View all ${rows.length}`);
  el.innerHTML = `
    <div class="panel${companiesExpanded ? " expanded" : ""}">
      <div class="panel-title">Companies with most open roles</div>
      ${
        rows.length
          ? renderBarList(rows, "domain", { clickable: true, limit: COMPANY_ROWS_SHOWN })
          : '<div class="sub" style="color:var(--grey)">No company has a matching open role.</div>'
      }
      ${
        rows.length > COMPANY_ROWS_SHOWN
          ? `<button type="button" class="bar-more" aria-expanded="${companiesExpanded}">${moreLabel()}</button>`
          : ""
      }
    </div>`;

  wireCompanyBarClicks(el);
  el.querySelector(".bar-more")?.addEventListener("click", (e) => {
    companiesExpanded = !companiesExpanded;
    const btn = e.currentTarget;
    btn.closest(".panel").classList.toggle("expanded", companiesExpanded);
    btn.setAttribute("aria-expanded", String(companiesExpanded));
    btn.textContent = moreLabel();
  });
}

// Clicking a company in any bar list filters the board to it. Shared by
// both grids, since the same rows now render in two places.
function wireCompanyBarClicks(el) {
  el.querySelectorAll("[data-company]").forEach((row) => {
    row.addEventListener("click", () => {
      state.company = [row.dataset.company];
      state.starred_only = false;
      state.offset = 0;
      msCompany.setSelected(state.company);
      loadJobs();
      loadTicker();
      window.scrollTo({ top: document.getElementById("board").offsetTop - 60, behavior: "smooth" });
    });
  });
}

// job board

let lastJobsResponse = null;

// All listings, Best matches, Saved. Derived from state rather than
// stored, so a link carrying ?skills= or ?starred=1 lands on the right
// view with nothing else to keep in step.
function currentView() {
  if (state.starred_only) return "saved";
  return state.skills.length ? "matches" : "all";
}

function paintViewSwitch() {
  const view = currentView();
  document.querySelectorAll("#view-switch [data-view]").forEach((b) => {
    const on = b.dataset.view === view;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
}

// The CV match, stated. A ranked board looks like an unranked one, so in
// Best matches this names what the order is built from and lets the
// reader drop a skill without leaving the board. Adding skills is the CV
// analyser's job, on /account, so this links there instead of growing a
// second editor.
function renderMatchPanel() {
  const panel = document.getElementById("match-panel");
  if (!panel) return;
  if (!state.skills.length || state.starred_only) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  const n = state.skills.length;
  panel.hidden = false;
  panel.innerHTML = `<span class="match-panel-label">Ranked by ${n} CV skill${n === 1 ? "" : "s"}</span>`
    // data-match-skill, not data-skill: renderJobRows wires every
    // [data-skill] on the page as "search for this skill", and that
    // handler stops propagation, so a shared attribute turned removing a
    // skill here into a text search for it.
    + state.skills.map((s) => `<button type="button" class="match-skill" data-match-skill="${escapeHtml(s)}"`
      + ` title="Stop matching on ${escapeHtml(s)}">${escapeHtml(s)} <span aria-hidden="true">✕</span></button>`).join("")
    + `<a class="link match-panel-edit" href="/account#cv">Edit skills</a>`;
}

// Best matches with nothing to rank by. Says what the view needs rather
// than showing a board that looks exactly like All listings.
function showMatchesPrompt() {
  const panel = document.getElementById("match-panel");
  panel.hidden = false;
  panel.innerHTML = getAuthTokens()
    ? `<span class="match-panel-label">Best matches orders listings by the skills on your CV.</span>`
      + ` <a class="link match-panel-edit" href="/account#cv">Analyze your CV</a>`
    : `<span class="match-panel-label">Best matches orders listings by the skills on your CV.`
      + ` Sign in, then analyze your CV on your account page.</span>`;
}

// The skills the CV analyser saved to the reader's profile. Null when
// signed out or when nothing has been saved yet.
async function profileSkills() {
  if (!getAuthTokens()) return null;
  try {
    const data = await authedFetch("/me/profile");
    const skills = (data && data.profile && data.profile.skills) || (data && data.skills) || [];
    return skills.length ? skills : null;
  } catch {
    return null;
  }
}

async function setView(view) {
  state.offset = 0;
  if (view === "saved") {
    state.starred_only = true;
    loadJobs();
    return;
  }
  state.starred_only = false;
  if (view === "all") {
    state.skills = [];
    if (state.sort === "match") setActiveSortHeader("age", "asc");
    loadJobs();
    loadTicker();
    return;
  }
  // Best matches keeps every filter already set: it orders what they
  // leave. Skills already on the board, from a link out of the CV
  // analyser or an earlier visit, are used as they are.
  if (!state.skills.length) {
    const skills = await profileSkills();
    if (!skills) {
      paintViewSwitch();
      showMatchesPrompt();
      return;
    }
    state.skills = skills;
  }
  setActiveSortHeader("match", "asc");
  loadJobs();
  loadTicker();
}

function renderCompanyChip() {
  const chip = document.getElementById("company-chip");
  if (state.company.length === 0) {
    chip.style.display = "none";
    return;
  }
  chip.style.display = "inline-flex";
  chip.querySelector(".chip-label").textContent =
    state.company.length === 1 ? state.company[0] : `${state.company.length} companies`;
}

// The filter (not sort/pagination) portion of state. Shared by loadJobs
// and the ticker, so "10 most recent" respects the active filters too.
function currentFilterParams() {
  return {
    search: state.search,
    department: state.department.join(","),
    seniority: state.seniority.join(","),
    company: state.company.join(","),
    country: state.country.join(","),
    city: state.city.join(","),
    workplace: state.workplace.join(","),
    skills: state.skills.join(","),
    // Rank, never filter. See job_filters.build_jobs_where.
    skills_mode: state.skills.length ? "rank" : "",
    confidence: state.confidence,
    max_age_days: state.max_age_days,
  };
}

// URL query-param syncing: filters/sort/page state and the open job
// drawer both round-trip through the URL, so a bookmarked or shared link
// reproduces the exact view. buildShareParams/shareUrl/syncUrl (write)
// pair with applyStateFromUrl/applyStateToFilterUI (read) below.
// replaceState throughout here, not pushState -- a filter change or page
// turn isn't a distinct "page" a user expects Back to undo one keystroke
// at a time. Opening a job IS treated as a real navigation (see
// openJobDetailAndPush), so that one pushes instead.

// Only ever set for a non-default value, so a plain visit to "/" stays
// a plain "/" instead of growing every field's default into the URL.
function buildShareParams() {
  const p = new URLSearchParams();
  if (state.search) p.set("search", state.search);
  if (state.department.length) p.set("department", state.department.join(","));
  if (state.seniority.length) p.set("seniority", state.seniority.join(","));
  if (state.company.length) p.set("company", state.company.join(","));
  if (state.country.length) p.set("country", state.country.join(","));
  if (state.city.length) p.set("city", state.city.join(","));
  if (state.workplace.length) p.set("workplace", state.workplace.join(","));
  if (state.skills.length) p.set("skills", state.skills.join(","));
  if (state.confidence !== "all") p.set("confidence", state.confidence);
  if (state.max_age_days) p.set("max_age_days", state.max_age_days);
  if (state.starred_only) p.set("starred", "1");
  if (state.sort !== "age") p.set("sort", state.sort);
  if (state.dir !== "asc") p.set("dir", state.dir);
  if (state.offset) p.set("offset", String(state.offset));
  if (selectedJobId) p.set("job", selectedJobId);
  return p;
}

function shareUrl() {
  const qsStr = buildShareParams().toString();
  return location.pathname + (qsStr ? `?${qsStr}` : "");
}

// Called from loadJobs() so every state-mutating handler (they all call
// loadJobs() right after) keeps the URL in step with zero extra wiring
// at each individual call site.
function syncUrl() {
  history.replaceState(null, "", shareUrl());
}

// Populates `state` from a query string -- location.search on boot, or
// whatever new URL a Back/Forward navigation hands back via popstate.
// Anything absent keeps state's existing default; an absent param means
// One gate for every filter value that reaches `state`, whatever it came
// from. Both sources are user-editable and neither was checked: a URL is
// hand-typed, shared and truncated, and localStorage is whatever a
// previous version of this file happened to leave there.
//
// Found live: ?max_age_days=abc sent "abc" straight to the API, which
// answers 400, so the board showed "Could not load jobs". That alone
// would be a bad link. But max_age_days is also persisted, so the junk
// was saved, and every later visit to the plain site reloaded it and
// broke again. A single malformed link locked a browser out of the board
// until its site data was cleared, with the only escape a Reset button
// inside a panel the reader has no reason to open.
//
// An invalid value is dropped, never clamped to something plausible:
// silently turning ?max_age_days=-5 into 30 would show results the URL
// did not ask for. The one exception is offset, which was already
// floored at 0 before this existed.
// "match" has no column header to click. It is not a column: it is what
// the board orders by while a CV match is on, set when the match arrives
// and dropped the moment someone clicks a real header.
const SORTABLE_KEYS = new Set(["age", "title", "match"]);

function cleanFilterValue(key, value) {
  switch (key) {
    case "max_age_days": {
      if (value === "" || value == null) return "";
      const s = String(value);
      return /^\d+$/.test(s) && Number(s) > 0 ? s : undefined;
    }
    case "sort":
      return SORTABLE_KEYS.has(value) ? value : undefined;
    case "dir":
      return value === "asc" || value === "desc" ? value : undefined;
    default:
      return value;
  }
}

// "default," not "clear," so a partial URL (just ?q=... say) doesn't
// stomp the rest back to defaults.
function applyStateFromUrl(search) {
  const p = new URLSearchParams(search);
  // A CV match is the exception to "default, not clear" above. It says
  // show me my matches, not show me my matches inside whatever I last
  // typed, and the filters it would otherwise inherit are saved ones the
  // reader cannot see from the link they just clicked.
  //
  // It also digs out a specific hole. The first version of this feature
  // sent the skills as q=, a single substring match, so everyone who
  // clicked See my matches before today has the phrase "Python Azure
  // Linux CI/CD Git Terraform..." saved as their text search. Reported
  // live: the match arrived, the chip appeared, and the board said NO
  // RESULTS, because the saved q was still intersecting it.
  //
  // Reset first, then merge, so the link's own params still land. A
  // shared ?skills=...&department=... keeps its department.
  if (p.has("skills")) {
    state.search = "";
    state.department = [];
    state.seniority = [];
    state.company = [];
    state.country = [];
    state.city = [];
    state.workplace = [];
    state.confidence = "all";
    state.max_age_days = "";
    state.starred_only = false;
  }
  if (p.has("search")) state.search = p.get("search");
  // Links older than the single box. Semicolons were the separator
  // there; here a space is, and quoting keeps a multi-word term whole.
  if (p.has("q") || p.has("keywords")) {
    state.search = [p.get("q") || "", ...(p.get("keywords") || "").split(";")]
      .map((t) => t.trim())
      .filter(Boolean)
      .map((t) => (t.includes(" ") ? `"${t}"` : t))
      .join(" ");
  }
  for (const key of ["department", "seniority", "company", "country", "city", "workplace", "skills"]) {
    if (p.has(key)) state[key] = p.get(key).split(",").filter(Boolean);
  }
  // A link from the CV analyser carries skills and no sort, and its
  // whole purpose is the ranking. Without this the board asks for
  // sort=age, the server honours it, and a ranked list comes back in
  // date order looking exactly like no match at all.
  if (p.has("skills") && !p.has("sort") && state.skills.length) state.sort = "match";
  if (p.has("confidence")) state.confidence = p.get("confidence");
  // Links older than the country filter. israel_only was replaced by
  // country=IL, and the two now return the same listings, so this is an
  // exact translation rather than the guess the old location param would
  // have needed. The API still answers israel_only for saved alerts.
  if (p.get("israel_only") === "1" && !state.country.includes("IL")) {
    state.country = [...state.country, "IL"];
  }
  if (p.has("max_age_days")) {
    const v = cleanFilterValue("max_age_days", p.get("max_age_days"));
    if (v !== undefined) state.max_age_days = v;
  }
  if (p.has("starred")) state.starred_only = p.get("starred") === "1";
  if (p.has("sort")) {
    const v = cleanFilterValue("sort", p.get("sort"));
    if (v !== undefined) state.sort = v;
  }
  if (p.has("dir")) {
    const v = cleanFilterValue("dir", p.get("dir"));
    if (v !== undefined) state.dir = v;
  }
  if (p.has("offset")) {
    const n = parseInt(p.get("offset"), 10);
    state.offset = Number.isFinite(n) && n > 0 ? n : 0;
  }
}

// Filters persisting across browser sessions -- requested directly: the
// URL round-trip above only reproduces a filter set that's actually IN
// the address bar (a shared/bookmarked link), so a plain revisit to "/"
// after closing the tab landed back on hardcoded defaults regardless of
// what was last picked. Same offset/job exclusions as buildShareParams,
// for the same reason (a fresh visit shouldn't resume on page 3, or with
// a job drawer open) -- everything else that's a real filter choice
// round-trips, sort/dir included.
const FILTERS_KEY = "iljobs_filters";
const PERSISTED_FILTER_KEYS = [
  "search", "department", "seniority", "company", "country", "city",
  "workplace", "skills", "confidence", "max_age_days", "starred_only",
  "sort", "dir",
];

function saveFiltersToStorage() {
  try {
    const saved = {};
    for (const key of PERSISTED_FILTER_KEYS) saved[key] = state[key];
    localStorage.setItem(FILTERS_KEY, JSON.stringify(saved));
  } catch {
    // Full quota or unavailable (private browsing) -- same as
    // setCachedJobs below, a pure UX nicety, not worth failing over.
  }
}

// Loaded as the new BASELINE before applyStateFromUrl runs, not after --
// that function only ever overrides keys actually present in the query
// string (see its own comment), so calling it second means an explicit
// URL param always wins over whatever's saved locally, never the other
// way around. Unknown/malformed storage (an older shape, a hand-edited
// value) is ignored key-by-key rather than rejecting the whole thing.
function applyStoredFilters() {
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(FILTERS_KEY) || "null");
  } catch {
    return;
  }
  if (!saved || typeof saved !== "object") return;
  // Saved by a version that had two boxes. Fold them into the one box
  // rather than dropping them: somebody had a search running, and this
  // is the visit where they find out it changed shape.
  // The same translation applyStateFromUrl does for an old link. Someone
  // browsing with Israel-only ticked should still be looking at Israeli
  // listings after the checkbox it lived in stops existing.
  if (saved.israel_only && !Array.isArray(saved.country)) {
    saved.country = ["IL"];
  } else if (saved.israel_only && !saved.country.includes("IL")) {
    saved.country = [...saved.country, "IL"];
  }
  if (!("search" in saved) && (saved.q || saved.keywords)) {
    saved.search = [saved.q || "", ...String(saved.keywords || "").split(";")]
      .map((t) => t.trim())
      .filter(Boolean)
      .map((t) => (t.includes(" ") ? `"${t}"` : t))
      .join(" ");
  }
  for (const key of PERSISTED_FILTER_KEYS) {
    if (!(key in saved)) continue;
    // Validated on the way out as well as in, so a browser already
    // holding a poisoned value from before this existed heals itself on
    // the next visit instead of needing its site data cleared.
    const value = cleanFilterValue(key, saved[key]);
    if (value !== undefined) state[key] = value;
  }
}

// Reflects `state` (just populated by applyStateFromUrl) into the actual
// filter controls. setSelected()/a direct .value assignment don't fire
// onChange, so this never double-triggers loadJobs() on its own -- the
// caller (boot, or the popstate handler) does that once, itself, after.
// Requires wireFilters() to have already run (msDepartment etc. assigned).
function applyStateToFilterUI() {
  document.getElementById("f-search").value = state.search;
  document.getElementById("f-date-posted").value = state.max_age_days || "";
  paintViewSwitch();
  msDepartment.setSelected(state.department);
  msSeniority.setSelected(state.seniority);
  msCompany.setSelected(state.company);
  msLocation.setSelected(state.country, state.city);
  msWorkplace.setSelected(state.workplace);
  setActiveSortHeader(state.sort, state.dir);
}

// Stale-while-revalidate for the job list: keyed by the exact
// filter/sort/page combo, so a revisit with the same view renders
// instantly from whatever was cached last time instead of sitting on a
// loading screen, while the real fetch still always runs in the
// background and silently replaces it the moment fresh data lands.
// Reported live: every visit meant a few seconds of "Loading
// listings..." even though the underlying data rarely changes that
// fast (the EventBridge-scheduled scrape-fast Lambda re-polls every 5
// min, most visits land well inside that window).
const JOBS_CACHE_PREFIX = "iljobs_jobs_cache:";

function getCachedJobs(params) {
  try {
    const raw = localStorage.getItem(JOBS_CACHE_PREFIX + params);
    const parsed = raw ? JSON.parse(raw) : null;
    // An empty cached result is treated as a miss, never rendered.
    // Reported live: the board showed "No listings match these filters"
    // with no filters set and 109,878 jobs available, because a single
    // empty response had been cached at some point and the cache-first
    // path renders whatever it finds. A stale result with jobs in it is
    // useful while the real one loads; a stale EMPTY one tells the user
    // something false and looks identical to a broken board. It also
    // outlives the transient that produced it forever, since nothing
    // ever overwrites it until a fetch succeeds. Skeletons instead.
    return parsed && Array.isArray(parsed.jobs) && parsed.jobs.length ? parsed : null;
  } catch {
    return null;
  }
}

function setCachedJobs(params, data) {
  try {
    // Same reasoning as getCachedJobs: never persist an empty result.
    // A genuine no-match is cheap to re-ask for and must not be able to
    // survive as a false "the board is empty" on the next visit.
    if (!data || !Array.isArray(data.jobs) || !data.jobs.length) return;
    localStorage.setItem(JOBS_CACHE_PREFIX + params, JSON.stringify(data));
  } catch {
    // Full quota or unavailable (private browsing) -- this is a pure UX
    // nicety, silently skip rather than break the real fetch over it.
  }
}

// Loading affordances. Three of them, each scoped to a different kind of
// wait, because one spinner for all of them is what makes an app feel
// slow: a full-screen blocker for a 200ms background refetch reads as
// "everything stopped" when nothing did.
//
//   skeleton rows  -> first load, nothing on screen yet
//   the load bar   -> a refetch while real results are still readable
//   .btn-busy      -> one control the user just clicked
const SKELETON_ROWS = 8;

function jobsSkeletonHtml(n = SKELETON_ROWS) {
  // Mirrors renderJobs' own row shape (star cell, logo + three stacked
  // lines, age cell) so the real rows land in the same places these
  // occupy and nothing jumps. aria-hidden throughout: a screen reader
  // gets the status line instead, not eight rows of nothing.
  return Array.from({ length: n }, () => `
    <tr class="skeleton-row" aria-hidden="true">
      <td><span class="skeleton sk-star"></span></td>
      <td class="title-cell">
        <span class="skeleton sk-logo"></span>
        <div class="job-card-body">
          <span class="skeleton sk-line sk-title"></span>
          <span class="skeleton sk-line sk-meta"></span>
          <span class="skeleton sk-line sk-links"></span>
        </div>
      </td>
      <td><span class="skeleton sk-age"></span></td>
    </tr>`).join("");
}

// Rides the topbar's own bottom rule (see .load-bar in style.css).
// Reference-counted: the board and the ticker refetch independently and
// often overlap, and the first one to finish shouldn't switch the bar
// off while the other is still in flight.
let inFlight = 0;
function setLoadBar(active) {
  inFlight = Math.max(0, inFlight + (active ? 1 : -1));
  const bar = document.getElementById("load-bar");
  if (bar) bar.classList.toggle("active", inFlight > 0);
}

// The precomputed default first page (loader/bootstrap.py), published by
// the merge Lambda and served straight from CloudFront's edge. Used only
// for the unfiltered default view, and only when its recorded params
// match exactly what this build was about to request, so the two can
// never drift into rendering something subtly wrong: any mismatch just
// falls through to a normal fetch.
//
// Worth it because api/db.py re-downloads the whole ~1.26GB jobs-read.db
// inside a user's request whenever its ETag moves. Measured across 809
// real requests: median 1.49s, p90 9.08s, max 25.00s, which is the API
// Lambda's own timeout. This file is 4.4KB gzipped and involves no
// Lambda at all.
let bootstrapPromise = null;
function getBootstrap() {
  // Started once, at module load, so it overlaps the rest of boot()
  // instead of queueing behind it. index.html preloads the same URL, so
  // this usually resolves from the browser's own preload cache.
  if (bootstrapPromise === null) {
    bootstrapPromise = fetch("/bootstrap.json")
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null); // missing or unreachable is a normal, expected state
  }
  return bootstrapPromise;
}

// Which request the page is currently waiting for.
//
// Reported live: type quickly and the board sticks on the previous
// filter. Nothing ordered the responses, so two requests in flight
// finished in whatever order the network gave them, and a slow one for
// the old filter painted over a fast one for the new. Reproduced with a
// stub: search box reading "bbb", rows reading "RESULT FOR aaa". A cold
// Lambda answering after a warm one does this for real.
//
// A counter rather than a timestamp: two calls in the same millisecond
// are exactly the case this has to separate. Every await in loadJobs is
// followed by a check, because any of them can be the point a newer
// call overtakes this one.
let jobsRequestSeq = 0;
let jobsInFlight = null;

async function loadJobs() {
  const seq = ++jobsRequestSeq;
  // Cancel rather than ignore. Ignoring would still cost the reader's
  // bandwidth and our Lambda invocation for an answer nobody will see,
  // and someone typing a ten-character search fires several of these.
  if (jobsInFlight) jobsInFlight.abort();
  const inFlight = new AbortController();
  jobsInFlight = inFlight;

  // Every state-mutating handler in this file calls loadJobs() right
  // after, so state is already final for this transition -- one call
  // here covers all of them instead of one at each call site.
  syncUrl();
  saveFiltersToStorage();
  updateFiltersToggleLabel();

  const tbody = document.getElementById("jobs-body");
  const starred = getStarred();
  renderCompanyChip();
  renderMatchPanel();
  paintViewSwitch();

  // Not awaited, and above the starred_only branch on purpose. The
  // listings are what the reader came for and the sidebar must never
  // hold them up, but "Saved" is a client-local view rather than an API
  // filter, so the Current search block is still answering for whatever
  // else is selected and would otherwise sit on the previous answer.
  refreshScopedStats();

  if (state.starred_only) {
    // Hands over this call's seq and controller rather than starting
    // its own, so a slow saved fetch loses to a newer view the same way
    // every other request here does.
    renderStarredOnly(starred, seq, inFlight);
    return;
  }

  document.getElementById("jobs-error").style.display = "none";
  document.getElementById("jobs-empty").style.display = "none";

  // Not awaited: the dropdown counts are secondary to the job list
  // itself, and refreshFacetOptions() has its own non-fatal fallback if
  // it's slow or fails.
  refreshFacetOptions();

  const params = qs({
    ...currentFilterParams(),
    sort: state.sort,
    dir: state.dir,
    limit: PAGE_SIZE,
    offset: state.offset,
  });

  let cached = getCachedJobs(params);

  // Nothing cached locally, but this might be the default view, which is
  // already sitting on the edge. Only await it when there's nothing
  // better to show, so a returning visitor never waits on it.
  if (!cached) {
    const boot = await getBootstrap();
    if (seq !== jobsRequestSeq) return; // a newer filter won while that resolved
    if (boot && boot.params === params && boot.jobs && boot.jobs.jobs.length) {
      cached = boot.jobs;
    }
  }

  tbody.closest("table").style.display = "";
  if (cached) {
    document.getElementById("jobs-loading").textContent = "";
    lastJobsResponse = cached;
    renderJobs(cached, starred);
    renderPagination(cached);
  } else {
    // Bones, not "Loading listings…". Same height as the rows about to
    // replace them, so the page doesn't reflow when data lands.
    tbody.innerHTML = jobsSkeletonHtml();
    document.getElementById("jobs-loading").textContent = "Loading listings";
  }

  setLoadBar(true);
  try {
    const data = await getJSON(`/jobs?${params}`, { signal: inFlight.signal });
    if (seq !== jobsRequestSeq) return;
    document.getElementById("jobs-loading").textContent = "";
    // Skip the re-render when the background refresh just confirms
    // nothing changed -- avoids a jarring flicker/scroll-reset for what
    // will be the common case (revisiting within the same 5-min window).
    const changed = !cached || JSON.stringify(data) !== JSON.stringify(cached);
    lastJobsResponse = data;
    if (changed) {
      renderJobs(data, starred);
      renderPagination(data);
    }
    setCachedJobs(params, data);
  } catch (err) {
    // An abort is this function cancelling itself, not a failure, and a
    // stale rejection belongs to a filter nobody is looking at.
    if (seq !== jobsRequestSeq || err.name === "AbortError") return;
    document.getElementById("jobs-loading").textContent = "";
    if (!cached) tbody.innerHTML = ""; // bones would otherwise sit there forever behind the error
    // A cached render is still on screen and still useful -- don't bury
    // it under an error banner over a transient fetch failure.
    if (!cached) {
      const errEl = document.getElementById("jobs-error");
      errEl.textContent = `Could not load jobs: ${err.message}`;
      errEl.style.display = "block";
    }
  } finally {
    // Always, never gated on the sequence. setLoadBar is reference
    // counted, and an earlier request skipping its decrement leaks the
    // count upward: request two increments before request one's finally
    // runs, one then declines to decrement, and the bar is stuck on for
    // the rest of the session. Guarding this looked like it was stopping
    // a stale request reporting the page as settled, but the counter
    // already handles that. Introduced by the sequencing fix and caught
    // in review before anyone saw it.
    setLoadBar(false);
  }
}

// Every empty state says the same two things: that there is nothing to
// show, then why.
function emptyState(line) {
  return `<strong>No results</strong><span>${line}</span>`;
}

// The saved view used to filter lastJobsResponse, which is whichever 50
// rows the board happens to be holding. Save a job, change one filter,
// open Saved, and it showed nothing. So it asks for the ids instead.
// include_closed/include_outdated go with them on purpose. A listing
// you saved three weeks ago and are still chasing is exactly the one
// the default filters would drop, and having it vanish from your own
// saved list is worse than showing it closed.
async function renderStarredOnly(starred, seq, inFlight) {
  document.getElementById("jobs-loading").textContent = "";
  document.getElementById("jobs-error").style.display = "none";
  document.getElementById("pagination").style.display = "none";
  const tbody = document.getElementById("jobs-body");
  const empty = document.getElementById("jobs-empty");

  // Stays synchronous for the nothing-saved case. No request, no bones,
  // no flicker on the way to an empty list.
  if (!starred.size) {
    empty.innerHTML = emptyState("You have not saved any listings yet.");
    empty.style.display = "block";
    tbody.innerHTML = "";
    document.getElementById("result-count").innerHTML = "";
    return;
  }
  empty.style.display = "none";

  // 200 is the endpoint's cap on ids. The tail of the set is the recent
  // end (toggleStar appends), so an outlier with 300 saved jobs sees the
  // 200 they starred most recently rather than the 200 they forgot.
  const ids = [...starred].slice(-200);
  const params = qs({
    ids: ids.join(","),
    include_closed: 1,
    include_outdated: 1,
    limit: 200,
  });

  tbody.closest("table").style.display = "";
  tbody.innerHTML = jobsSkeletonHtml();
  document.getElementById("jobs-loading").textContent = "Loading listings";
  document.getElementById("result-count").innerHTML = "";
  setLoadBar(true);
  try {
    const data = await getJSON(`/jobs?${params}`, { signal: inFlight.signal });
    if (seq !== jobsRequestSeq) return;
    document.getElementById("jobs-loading").textContent = "";
    // Same bookkeeping renderJobs does, so clicking a saved row opens
    // the detail panel from data already in hand instead of refetching.
    lastJobsResponse = data;
    const rows = data.jobs || [];
    if (!rows.length) {
      // Saved ids that the API no longer knows at all. Rare, and it
      // reads as a bug if the view just goes blank.
      empty.innerHTML = emptyState("None of your saved listings are available any more.");
      empty.style.display = "block";
      tbody.innerHTML = "";
      return;
    }
    renderJobRows(rows, starred);
    document.getElementById("result-count").innerHTML = `<b>${rows.length}</b> saved`;
  } catch (err) {
    if (seq !== jobsRequestSeq || err.name === "AbortError") return;
    document.getElementById("jobs-loading").textContent = "";
    tbody.innerHTML = ""; // bones would otherwise sit there forever behind the error
    const errEl = document.getElementById("jobs-error");
    errEl.textContent = `Could not load your saved listings: ${err.message}`;
    errEl.style.display = "block";
  } finally {
    setLoadBar(false); // see the note in loadJobs: never gate this
  }
}

// Which skills the server matched on, straight from the response rather
// than from state: the two can differ for a moment during a refetch, and
// marking a chip that did not actually put the row here is a small lie
// in the one place the reader is looking for an explanation.
let matchedSkills = new Set();

function renderJobs(data, starred) {
  matchedSkills = new Set(data.matched_skills || []);
  document.getElementById("pagination").style.display = "flex";
  if (!data.jobs.length) {
    document.getElementById("jobs-empty").innerHTML = emptyState("No listings match these filters.");
    document.getElementById("jobs-empty").style.display = "block";
    document.getElementById("jobs-body").innerHTML = "";
    document.getElementById("result-count").innerHTML = "";
    return;
  }
  document.getElementById("jobs-empty").style.display = "none";
  renderJobRows(data.jobs, starred);
  const from = state.offset + 1;
  const to = Math.min(state.offset + data.jobs.length, data.total);
  document.getElementById("result-count").innerHTML =
    `<b>${from}–${to}</b> of <b>${fmtInt(data.total)}</b> open listings`
    + (state.skills.length && state.sort === "match" ? ", best matches first" : "");
}

// "Company · Department · Location (Workplace)" -- one scannable line
// under the title, LinkedIn-card style, standing in for what used to be
// three separate table columns (Company was already folded in as the
// row's ".company" div; Location and Category had their own <td>s).
// What to actually call a company on screen. company_domain is an
// internal key, not an identity: discovery guesses {ats-token}.com and
// keeps the guess even when it resolves to nothing, so a fifth of them
// are hosts that never existed (headoutcareers.com for Headout,
// informagroupplc.com for Informa Group Plc.). company_name is the name
// the company's own ATS reports, resolved once by
// resolve_company_names.py. Falls back to the domain for Lever and
// Workday, which expose no name anywhere, about 1% of companies.
function companyLabel(j) {
  return j.company_name || j.company_domain;
}

function jobMetaLine(j) {
  // The company gets its own span so it can carry more contrast than the
  // rest of the line. Everything here used to be one flat grey, so the
  // employer read with exactly the same weight as the department it
  // happens to be hiring into, and a reader scanning the column had
  // nothing to land on between the title and the location.
  const parts = [`<span class="job-company">${escapeHtml(companyLabel(j))}</span>`];
  if (j.department) parts.push(escapeHtml(j.department));
  if (j.location) parts.push(escapeHtml(j.location));
  let line = parts.join(" · ");
  if (j.workplace_type) line += ` (${escapeHtml(WORKPLACE_LABELS[j.workplace_type] || j.workplace_type)})`;
  return line;
}

// What actually stands behind the number, in the reader's own terms.
// Three sources with genuinely different evidence, and the difference
// that matters most to someone reading it is whether the employer said
// this or we did. Both estimates say so outright rather than leaving it
// to the "Est." prefix.
// Both estimates are monthly gross, so both say so. A disclosed figure
// is quoted exactly as the employer wrote it, which may be annual or
// hourly, so its note claims no period at all.
const SALARY_SOURCE_NOTE = {
  disclosed: "Published by the employer on this listing, in their own terms.",
  table: "Our estimate of monthly gross pay, from Israeli market rates for this role and seniority. Not the employer's own figure.",
  estimated: "Our estimate of monthly gross pay, from what comparable roles actually pay at this company and location. Not the employer's own figure.",
};

// Real disclosed comp shown plainly; an estimate prefixed "Est." and
// never given the weight of a real number. Neither present just reads
// "Undisclosed," matching this board's principle of showing an absence
// as an absence rather than hiding it.
//
// Falls back to the older salary_is_estimate boolean, because a cached
// page or a bootstrap.json written before salary_source existed will
// arrive without it, and "table" is what every estimate was then.
function jobSalaryHtml(j) {
  if (!j.salary_text) return `<span class="job-salary undisclosed">Undisclosed</span>`;
  const source = j.salary_source || (j.salary_is_estimate ? "table" : "disclosed");
  const isEstimate = source !== "disclosed";
  const note = SALARY_SOURCE_NOTE[source] || SALARY_SOURCE_NOTE.estimated;
  const cls = isEstimate ? `job-salary estimate ${escapeHtml(source)}` : "job-salary";
  return `<span class="${cls}" data-salary-source="${escapeHtml(source)}" title="${escapeHtml(note)}">`
    + `${isEstimate ? "Est. " : ""}${escapeHtml(j.salary_text)}</span>`;
}

// Plain text, no chip/badge container (confirmed live) -- each one
// still a real button, clicking sets the board's existing `keywords`
// filter (the same AND-match param /api/jobs already supports) to that
// one term and reloads, rather than adding a second, parallel filter
// mechanism just for this.
// The skills that put this row on the list, shown only while a CV match
// is on. The full chip list was pulled from rows on request, to give
// salary the space, and this does not bring it back: a row shows the
// two or three of your own skills it shares, or nothing at all. Without
// it a ranked board is indistinguishable from an unranked one, and the
// order looks arbitrary rather than earned.
//
// It says how many, and which of the listing's own skills the CV lacks,
// as plain counts. No percentage: the ranking is a count of shared skill
// tags, and a score dressed up as more than that would be the one
// unexplainable thing on the row.
function jobMatchHtml(j) {
  if (!matchedSkills.size) return "";
  const listed = (j.skills || "").split(",").filter(Boolean);
  const hits = listed.filter((s) => matchedSkills.has(s));
  if (!hits.length) return "";
  const asks = listed.filter((s) => !matchedSkills.has(s));
  return `<div class="job-match">`
    + `<span class="job-match-count">${hits.length} of your ${matchedSkills.size} skills</span>`
    + hits.map((s) => `<span class="match-chip">${escapeHtml(s)}</span>`).join("")
    + (asks.length ? `<span class="job-match-asks">Also asks for ${asks.map(escapeHtml).join(", ")}</span>` : "")
    + `</div>`;
}

function jobSkillsHtml(j) {
  const skills = (j.skills || "").split(",").filter(Boolean);
  if (!skills.length) return "";
  const chips = skills
    .map((s) => `<button class="skill-chip" data-skill="${escapeHtml(s)}" type="button">${escapeHtml(s)}</button>`)
    .join("");
  return `<span class="skill-bracket">[</span>${chips}<span class="skill-bracket">]</span>`;
}

function renderJobRows(jobs, starred) {
  // The tooltip stopped being true once /me/saved existed. Signed in,
  // the star does follow you, and saying otherwise talks people out of
  // using it.
  const starTitle = getAuthTokens() ? "Save to your account" : "Save (this browser only)";
  document.getElementById("jobs-body").innerHTML = jobs
    .map((j) => {
      const age = j.posted_at
        ? (Date.now() - new Date(j.posted_at).getTime()) / 86400000
        : null;
      const fresh = age !== null && age <= 3;
      const isStarred = starred.has(j.id);
      return `
      <tr data-id="${j.id}" class="${j.id === selectedJobId ? "selected" : ""}">
        <td>
          <button class="star-btn ${isStarred ? "on" : ""}" data-star="${j.id}" title="${starTitle}">
            ${isStarred ? "★" : "☆"}
          </button>
        </td>
        <td class="title-cell">
          ${companyLogoImg(j.company_domain, 64, "listing", j.logo_url)}
          <div class="job-card-body">
            <div class="job-card-title">
              <a href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener">${escapeHtml(j.title)}</a>
              ${j.seniority ? `<span class="badge seniority">${escapeHtml(SENIORITY_LABELS[j.seniority] || j.seniority)}</span>` : ""}
              ${j.confidence === "best_effort" ? '<span class="badge best-effort" title="Scraped from the company\'s own page, not a live ATS API">best_effort</span>' : ""}
              ${j.closed_at ? '<span class="badge closed" title="This listing is no longer open">Closed</span>' : ""}
            </div>
            <div class="job-meta">${jobMetaLine(j)}</div>
            ${jobMatchHtml(j)}
            <div class="job-links">
              <a class="apply-link" href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener" title="Open the original listing to apply">Apply ↗</a>
              <button class="copy-link-btn" data-copy-url="${escapeHtml(j.url || "")}" title="Copy the application link">Save link</button>
            </div>
          </div>
          <div class="job-salary-col">${jobSalaryHtml(j)}</div>
          <!-- Skills chips pulled from the UI for now, per request, while
               salary gets more attention -- jobSkillsHtml/.skill-chip and
               its click-to-filter wiring are still intact below, just
               unused, so this is a one-line change to bring back. -->
        </td>
        <td data-label="Age" class="age-cell ${fresh ? "fresh" : ""}">${fmtAge(age)}</td>
      </tr>`;
    })
    .join("");

  document.querySelectorAll("[data-star]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // inside a now-clickable <tr> (opens the detail panel); starring shouldn't also open it
      const s = toggleStar(btn.dataset.star);
      btn.classList.toggle("on", s.has(btn.dataset.star));
      btn.textContent = s.has(btn.dataset.star) ? "★" : "☆";
      syncDetailStarButton(btn.dataset.star, s);
      pushStar(btn.dataset.star, s.has(btn.dataset.star));
      if (state.starred_only) loadJobs();
    });
  });

  document.querySelectorAll("[data-copy-url]").forEach((btn) => {
    btn.addEventListener("click", () => copyToClipboard(btn, btn.dataset.copyUrl));
  });

  document.querySelectorAll("[data-skill]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // same reasoning as the star button above
      // Quoted, so "REST API" stays one term rather than two words the
      // listing must both mention separately.
      state.search = btn.dataset.skill.includes(" ") ? `"${btn.dataset.skill}"` : btn.dataset.skill;
      state.offset = 0;
      document.getElementById("f-search").value = state.search;
      loadJobs();
      loadTicker();
      window.scrollTo({ top: document.getElementById("board").offsetTop - 60, behavior: "smooth" });
    });
  });
}

async function copyToClipboard(btn, url) {
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
  } catch {
    // Clipboard API needs a secure context (https, or localhost) and
    // isn't guaranteed everywhere. Fall back to a hidden textarea copy
    // rather than silently failing on older/locked-down browsers.
    const ta = document.createElement("textarea");
    ta.value = url;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  }
  // innerHTML, not textContent -- a plain-text button (Save link) round-trips
  // through either the same way, but an icon-only button (an inline <svg>,
  // no text content at all) needs innerHTML or the restore below would wipe
  // the icon out instead of bringing it back.
  const original = btn.innerHTML;
  btn.textContent = "Copied";
  btn.classList.add("copied");
  setTimeout(() => {
    btn.innerHTML = original;
    btn.classList.remove("copied");
  }, 1200);
}

function renderPagination(data) {
  const el = document.getElementById("pagination-pages");
  const totalPages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  const current = Math.floor(state.offset / PAGE_SIZE) + 1;
  const start = Math.max(1, current - 3);
  const end = Math.min(totalPages, start + 6);

  let html = `<button ${current === 1 ? "disabled" : ""} data-page="${current - 1}">‹</button>`;
  for (let p = start; p <= end; p++) {
    html += `<button class="${p === current ? "active" : ""}" data-page="${p}">${p}</button>`;
  }
  html += `<button ${current === totalPages ? "disabled" : ""} data-page="${current + 1}">›</button>`;
  el.innerHTML = html;

  el.querySelectorAll("[data-page]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.offset = (parseInt(btn.dataset.page, 10) - 1) * PAGE_SIZE;
      loadJobs();
      window.scrollTo({ top: document.getElementById("board").offsetTop - 60, behavior: "smooth" });
    });
  });
}

// job detail panel: opens beside the list on selecting a row (never a
// modal). Renders instantly from the row's already-known fields, then
// fills in `description` once GET /api/jobs/{id} resolves, since the list
// endpoint doesn't carry full descriptions.

function findKnownJob(id) {
  return lastJobsResponse?.jobs?.find((j) => j.id === id) || null;
}

// Keeps the panel's own star button in step with whichever table row was
// clicked (either direction: starring from the row, or from the panel).
function syncDetailStarButton(id, starredSet) {
  if (id !== selectedJobId) return;
  const btn = document.querySelector(".job-detail-star");
  if (!btn) return;
  const on = starredSet.has(id);
  btn.classList.toggle("on", on);
  btn.textContent = on ? "★ Saved" : "☆ Save";
}

// probe.py's _clean_text marks section headings with a leading "## ".
// Render those bold rather than showing the marker literally. Every line
// is still escaped individually, so nothing in the source text is ever
// treated as markup.
function renderDescriptionLines(description) {
  return description
    .split("\n")
    .map((line) =>
      line.startsWith("## ")
        ? `<strong class="job-detail-desc-heading">${escapeHtml(line.slice(3))}</strong>`
        : escapeHtml(line)
    )
    .join("\n");
}

// Hand-written, not an icon font/library -- this project has zero
// external dependencies anywhere in the frontend, and one glyph doesn't
// change that. currentColor so it inherits the button's own ink/hover
// color for free, same as every other flat, monochrome control here.
const LINK_ICON_SVG = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.07 0l2.83-2.83a5 5 0 0 0-7.07-7.07L11.5 4.5"/><path d="M14 11a5 5 0 0 0-7.07 0L4.1 13.83a5 5 0 0 0 7.07 7.07L13.5 19.5"/></svg>`;

// Deep link to one job, independent of whatever filters are currently
// active -- see openJobDetail's "not in the current list" fallback,
// which is what makes opening this link from cold actually work.
function jobPermalink(id) {
  return `${location.origin}${location.pathname}?job=${encodeURIComponent(id)}`;
}

function renderJobDetailBody(job, { descriptionLoading = false, descriptionError = null } = {}) {
  const age = job.posted_at ? (Date.now() - new Date(job.posted_at).getTime()) / 86400000 : null;
  const starred = getStarred().has(job.id);

  let descriptionHtml;
  if (descriptionError) {
    descriptionHtml = `<div class="error-state">Could not load the full description: ${escapeHtml(descriptionError)}</div>`;
  } else if (descriptionLoading) {
    descriptionHtml = `<div class="loading-state">Loading description…</div>`;
  } else if (job.description) {
    descriptionHtml = `<div class="job-detail-description">${renderDescriptionLines(job.description)}</div>`;
  } else {
    descriptionHtml = `<div class="job-detail-description empty">No description provided by this listing.</div>`;
  }

  return `
    <div class="job-detail-header">
      <div>
        <div class="job-detail-company">${companyLogoImg(job.company_domain, 64, "detail", job.logo_url)}${escapeHtml(companyLabel(job))}</div>
        <h3 class="job-detail-title">${escapeHtml(job.title)}</h3>
        <div class="job-detail-badges">
          ${job.seniority ? `<span class="badge seniority">${escapeHtml(SENIORITY_LABELS[job.seniority] || job.seniority)}</span>` : ""}
          ${job.workplace_type ? `<span class="badge workplace">${escapeHtml(WORKPLACE_LABELS[job.workplace_type] || job.workplace_type)}</span>` : ""}
          ${job.confidence === "best_effort" ? '<span class="badge best-effort" title="Scraped from the company\'s own page, not a live ATS API">best_effort</span>' : ""}
          ${job.closed_at ? '<span class="badge">Closed</span>' : ""}
        </div>
      </div>
      <div class="job-detail-header-actions">
        <button type="button" class="job-detail-icon-btn" data-copy-permalink="${escapeHtml(jobPermalink(job.id))}" title="Copy link to this job" aria-label="Copy link to this job">${LINK_ICON_SVG}</button>
        <button type="button" class="job-detail-icon-btn job-detail-close" title="Close" aria-label="Close job detail">✕</button>
      </div>
    </div>

    <!-- No copy-link/"Save link" button here anymore -- redundant with
         the header's own copy-link icon next to ✕ above. The row-level
         one (renderJobRows) copies something different (the external
         apply URL, not this page's permalink) and stays. -->
    <div class="job-detail-actions">
      <a class="job-detail-apply" href="${escapeHtml(job.url || "#")}" target="_blank" rel="noopener" title="Open the original listing to apply">Apply ↗</a>
      <button type="button" class="job-detail-star ${starred ? "on" : ""}" data-star="${job.id}">${starred ? "★ Saved" : "☆ Save"}</button>
    </div>

    <div class="job-detail-meta">
      <div class="job-detail-meta-row"><span class="label">Location</span><span class="value">${escapeHtml(job.location || "-")}</span></div>
      <div class="job-detail-meta-row"><span class="label">Category</span><span class="value">${escapeHtml(job.category || "-")}</span></div>
      ${job.department ? `<div class="job-detail-meta-row"><span class="label">Department</span><span class="value">${escapeHtml(job.department)}</span></div>` : ""}
      <div class="job-detail-meta-row"><span class="label">Seniority</span><span class="value">${escapeHtml(SENIORITY_LABELS[job.seniority] || job.seniority || "-")}</span></div>
      <div class="job-detail-meta-row"><span class="label">Workplace</span><span class="value">${escapeHtml(WORKPLACE_LABELS[job.workplace_type] || job.workplace_type || "-")}</span></div>
      <div class="job-detail-meta-row"><span class="label">Posted</span><span class="value">${age !== null ? `${fmtAge(age)} ago` : "-"}</span></div>
      <div class="job-detail-meta-row"><span class="label">Via</span><span class="value">${escapeHtml(job.ats || "-")}</span></div>
    </div>

    <div class="job-detail-description-title">Description</div>
    ${descriptionHtml}`;
}

function wireJobDetailPanel(job) {
  const panel = document.getElementById("job-detail");
  panel.querySelector(".job-detail-close").addEventListener("click", closeJobDetailAndSync);
  panel.querySelector(".job-detail-star").addEventListener("click", (e) => {
    const s = toggleStar(job.id);
    const on = s.has(job.id);
    e.currentTarget.classList.toggle("on", on);
    e.currentTarget.textContent = on ? "★ Saved" : "☆ Save";
    const rowBtn = document.querySelector(`[data-star="${job.id}"].star-btn`);
    if (rowBtn) {
      rowBtn.classList.toggle("on", on);
      rowBtn.textContent = on ? "★" : "☆";
    }
    pushStar(job.id, on);
    if (state.starred_only) loadJobs();
  });
  const permalinkBtn = panel.querySelector("[data-copy-permalink]");
  if (permalinkBtn) permalinkBtn.addEventListener("click", () => copyToClipboard(permalinkBtn, permalinkBtn.dataset.copyPermalink));
}

async function openJobDetail(id) {
  const panel = document.getElementById("job-detail");
  // Not always in the currently loaded/filtered page -- a deep link (see
  // jobPermalink/applyStateFromUrl) can point at a job that isn't on
  // this view at all. When it's on-screen, its row data renders
  // immediately as a placeholder while the full fetch is in flight, same
  // as before; when it isn't, there's nothing to render early, just a
  // loading state, and no row to highlight.
  const known = findKnownJob(id);

  const previousId = selectedJobId;
  selectedJobId = id;
  document.querySelector(`tr[data-id="${previousId}"]`)?.classList.remove("selected");
  if (known) document.querySelector(`tr[data-id="${id}"]`)?.classList.add("selected");

  clearTimeout(jobDetailCloseTimer);
  panel.hidden = false;
  panel.innerHTML = known
    ? renderJobDetailBody(known, { descriptionLoading: true })
    : `<div class="loading-state">Loading job…</div>`;
  if (known) wireJobDetailPanel(known);

  // Slide it in on the next frame. The class goes on after hidden=false
  // has painted, or there's no off-screen starting position for the
  // transition to animate from. The page behind is deliberately not
  // scroll-locked, so the board keeps scrolling under the pointer (see
  // .job-detail in style.css).
  requestAnimationFrame(() => {
    panel.classList.add("open");
    document.getElementById("job-scrim")?.classList.add("open");
  });

  try {
    const full = await getJSON(`/jobs/${encodeURIComponent(id)}`);
    if (selectedJobId !== id) return; // a different row was picked while this was in flight
    panel.innerHTML = renderJobDetailBody(full);
    wireJobDetailPanel(full);
  } catch (err) {
    if (selectedJobId !== id) return;
    if (known) {
      panel.innerHTML = renderJobDetailBody(known, { descriptionError: err.message });
      wireJobDetailPanel(known);
    } else if (/no job with that id/i.test(err.message)) {
      // A shared or bookmarked link to a listing that has left the board.
      // It used to read "Could not load this job: no job with that id",
      // which sounds like the site broke rather than the role closing.
      panel.innerHTML = `<div class="job-gone">
        <p class="job-gone-label">Listing not found</p>
        <p>This listing is no longer on the board. Roles get filled and taken down, so links to them go stale.</p>
        <button type="button" class="btn ghost btn-small" data-gone-close>Back to listings</button>
      </div>`;
      panel.querySelector("[data-gone-close]").addEventListener("click", closeJobDetailAndSync);
    } else {
      panel.innerHTML = `<div class="error-state">Could not load this job: ${escapeHtml(err.message)}</div>`;
    }
  }
}

// Wraps openJobDetail for a real, user-initiated navigation (a row
// click, or restoring a deep link on boot) -- pushes a new history
// entry so Back closes the drawer, same expectation as any other
// permalink-backed detail view. The popstate handler below calls
// openJobDetail directly instead, since the URL there already changed
// out from under it; pushing again would double up the history stack.
async function openJobDetailAndPush(id) {
  await openJobDetail(id);
  if (selectedJobId === id) history.pushState(null, "", shareUrl());
}

function closeJobDetail() {
  const panel = document.getElementById("job-detail");
  panel.classList.remove("open");
  document.getElementById("job-scrim")?.classList.remove("open");
  // Delayed to match style.css's 0.25s slide-out transition -- an
  // immediate hidden=true would cut straight to display:none, same as
  // no animation at all. Cleared by the next openJobDetail (see its
  // own comment) so switching jobs mid-close can't get yanked shut.
  jobDetailCloseTimer = setTimeout(() => {
    panel.hidden = true;
    panel.innerHTML = "";
  }, 250);
  document.querySelector(`tr[data-id="${selectedJobId}"]`)?.classList.remove("selected");
  selectedJobId = null;
}

// Same reasoning as openJobDetailAndPush -- closing via the header
// button is a real, user-initiated step back, so the URL drops ?job=
// right away rather than waiting for the next filter change to notice.
function closeJobDetailAndSync() {
  closeJobDetail();
  syncUrl();
}

function wireJobDetail() {
  document.getElementById("jobs-body").addEventListener("click", (e) => {
    if (e.target.closest("a, button")) return; // Apply/Save link/star handle their own click
    const row = e.target.closest("tr[data-id]");
    if (row) openJobDetailAndPush(row.dataset.id);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && selectedJobId !== null) closeJobDetailAndSync();
  });

  // Clicking the dimmed board closes the sheet. Standard for anything
  // covering the page, and it's the nearest target: the close button is over on the far side of
  // the sheet, but the thing the reader is looking at is the list.
  document.getElementById("job-scrim")?.addEventListener("click", () => {
    if (selectedJobId !== null) closeJobDetailAndSync();
  });

  wireJobDetailSwipe();
}

// Requested live: the sheet takes the whole screen on mobile, so
// closing it should also work as a swipe, not just tapping the small X
// in the corner. Gated to that full-screen variant, since it drags on
// translateY and the side sheet above 960px slides on translateX. Only cares about a drag starting on the panel's own
// header (job-detail-actions and above -- the description/skills area
// below has its own vertical scroll to preserve, so a swipe starting
// there would fight it), and only a downward drag by more than a
// quarter of the panel's own height counts as "close" -- anything
// short of that snaps back, same as any native bottom-sheet gesture.
function wireJobDetailSwipe() {
  const panel = document.getElementById("job-detail");
  let startY = null;
  let dragging = false;

  panel.addEventListener("touchstart", (e) => {
    if (!MOBILE_SHEET_QUERY.matches || !panel.classList.contains("open")) return;
    if (!e.target.closest(".job-detail-actions, .job-detail-meta")) return;
    startY = e.touches[0].clientY;
    dragging = true;
    panel.style.transition = "none";
  }, { passive: true });

  panel.addEventListener("touchmove", (e) => {
    if (!dragging) return;
    const delta = e.touches[0].clientY - startY;
    if (delta <= 0) return; // upward: not a close gesture, let it sit at rest
    panel.style.transform = `translateY(${delta}px)`;
  }, { passive: true });

  panel.addEventListener("touchend", (e) => {
    if (!dragging) return;
    dragging = false;
    panel.style.transition = "";
    const delta = e.changedTouches[0].clientY - startY;
    panel.style.transform = "";
    if (delta > panel.getBoundingClientRect().height * 0.25) {
      closeJobDetailAndSync();
    }
  });
}

// multi-select filter dropdown (Category / Level / Company)

// One open dropdown at a time. Opening a second one closes whichever
// was already open, same as a native <select> would behave.
const OPEN_MULTISELECTS = new Set();

function createMultiSelect(containerId, { placeholder, options = [], searchable = false, onChange, pinnedOption = null }) {
  const container = document.getElementById(containerId);
  const selected = new Set();
  let currentOptions = options;

  container.innerHTML = `
    <button type="button" class="ms-toggle" aria-haspopup="listbox" aria-expanded="false">${escapeHtml(placeholder)}</button>
    <div class="ms-menu" hidden>
      ${
        pinnedOption
          ? `<label class="ms-option ms-pinned">
               <input type="checkbox" id="${pinnedOption.id}" ${pinnedOption.checked ? "checked" : ""} />
               ${escapeHtml(pinnedOption.label)}
             </label>
             <div class="ms-pinned-divider"></div>`
          : ""
      }
      ${searchable ? '<input type="text" class="ms-search" placeholder="Filter…" />' : ""}
      <div class="ms-options" role="listbox"></div>
      <button type="button" class="ms-clear">Clear</button>
    </div>
  `;
  const toggle = container.querySelector(".ms-toggle");
  const menu = container.querySelector(".ms-menu");
  const optionsEl = container.querySelector(".ms-options");
  const searchEl = container.querySelector(".ms-search");

  if (pinnedOption) {
    container.querySelector(`#${pinnedOption.id}`).addEventListener("change", (e) => {
      pinnedOption.onChange(e.target.checked);
    });
  }

  function renderOptions(filterText = "") {
    const q = filterText.trim().toLowerCase();
    const visible = q ? currentOptions.filter((o) => o.label.toLowerCase().includes(q)) : currentOptions;
    optionsEl.innerHTML =
      visible
        .map(
          (o) => `
        <label class="ms-option">
          <input type="checkbox" value="${escapeHtml(o.value)}" ${selected.has(o.value) ? "checked" : ""} />
          ${escapeHtml(o.label)}
        </label>`
        )
        .join("") || '<div class="ms-empty">No matches.</div>';
  }

  function updateLabel() {
    if (selected.size === 0) {
      toggle.textContent = placeholder;
    } else if (selected.size === 1) {
      const opt = currentOptions.find((o) => o.value === [...selected][0]);
      toggle.textContent = opt ? opt.label : [...selected][0];
    } else {
      toggle.textContent = `${selected.size} selected`;
    }
    toggle.classList.toggle("active", selected.size > 0);
  }

  function close() {
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    OPEN_MULTISELECTS.delete(close);
  }

  function open() {
    OPEN_MULTISELECTS.forEach((closeOther) => closeOther());
    menu.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    OPEN_MULTISELECTS.add(close);
    if (searchEl) searchEl.focus();
  }

  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.hidden ? open() : close();
  });
  menu.addEventListener("click", (e) => e.stopPropagation()); // clicks inside the menu shouldn't bubble to document and self-close it

  optionsEl.addEventListener("change", (e) => {
    if (!e.target.matches('input[type="checkbox"]')) return;
    e.target.checked ? selected.add(e.target.value) : selected.delete(e.target.value);
    updateLabel();
    onChange([...selected]);
  });

  if (searchEl) searchEl.addEventListener("input", () => renderOptions(searchEl.value));

  container.querySelector(".ms-clear").addEventListener("click", () => {
    selected.clear();
    renderOptions(searchEl ? searchEl.value : "");
    updateLabel();
    onChange([]);
  });

  renderOptions();
  updateLabel();

  return {
    setOptions(opts) {
      const current = [...selected];
      currentOptions = opts;
      // Drop any selected value no longer in the new option set (e.g.
      // Location narrowing to IL-only, dropping a non-IL pick).
      selected.clear();
      current.filter((v) => opts.some((o) => o.value === v)).forEach((v) => selected.add(v));
      renderOptions(searchEl ? searchEl.value : "");
      updateLabel();
      // Tell the caller if something was silently dropped, so its state
      // doesn't keep sending a value this widget no longer shows selected.
      if (selected.size !== current.length) onChange([...selected]);
    },
    reset() {
      selected.clear();
      renderOptions("");
      updateLabel();
      if (searchEl) searchEl.value = "";
    },
    setSelected(values) {
      selected.clear();
      values.forEach((v) => selected.add(v));
      renderOptions(searchEl ? searchEl.value : "");
      updateLabel();
    },
  };
}

// The Locations dropdown: countries at the top level, each country's own
// cities folded underneath it.
//
// A sibling of createMultiSelect above rather than a mode inside it.
// Almost nothing survives the jump: two independent selection sets
// instead of one, expansion state no flat caller has, a two-argument
// setSelected, an onChange that hands back a pair. Every internal of the
// flat widget would have grown a grouped/flat branch for the benefit of
// one caller while its ten flat callers paid for branches they never
// take. The one thing the two genuinely share is the one-open-at-a-time
// contract, and that already lives outside both, in OPEN_MULTISELECTS.
//
// Country and city are independent filters that AND together on the
// server, so ticking a country here deliberately does not tick its
// cities and ticking a city does not tick its country. country=IL with
// city=Berlin is a legitimate way to ask for nothing.
//
// Same drawing as the chevron on .filters select (see style.css), so the
// two arrows sitting a few pixels apart in the filter row match. Inline
// here rather than a background-image data URI, because currentColor
// only follows the element's color when the SVG is really in the DOM.
const MS_CHEVRON_SVG =
  '<svg viewBox="0 0 10 6" width="10" height="6" aria-hidden="true">'
  + '<path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5"'
  + ' stroke-linecap="round" stroke-linejoin="round"/></svg>';

function createLocationSelect(containerId, { placeholder, onChange }) {
  const container = document.getElementById(containerId);
  const selectedCountries = new Set();
  const selectedCities = new Set();
  // Which countries are open. View state, not filter state: it never
  // reaches the URL or localStorage, and a Back/Forward navigation has no
  // business re-collapsing a country somebody just opened.
  const expanded = new Set();
  let countries = [];

  container.innerHTML = `
    <button type="button" class="ms-toggle" aria-haspopup="listbox" aria-expanded="false">${escapeHtml(placeholder)}</button>
    <div class="ms-menu" hidden>
      <input type="text" class="ms-search" placeholder="Filter…" />
      <div class="ms-options" role="listbox"></div>
      <button type="button" class="ms-clear">Clear</button>
    </div>
  `;
  const toggle = container.querySelector(".ms-toggle");
  const menu = container.querySelector(".ms-menu");
  const optionsEl = container.querySelector(".ms-options");
  const searchEl = container.querySelector(".ms-search");

  function optionHtml(kind, row, checked) {
    const count = row.n == null ? "" : ` (${fmtInt(row.n)})`;
    return `
      <label class="ms-option ms-${kind}">
        <input type="checkbox" data-kind="${kind}" value="${escapeHtml(row.value)}" ${checked ? "checked" : ""} />
        <span class="ms-option-text">${escapeHtml(row.label)}${count}</span>
      </label>`;
  }

  // Only the search box's own value, so a country the search opened can
  // still be collapsed by hand while that same search is still typed.
  // Auto-expanding on every render instead would make the chevron a
  // no-op for exactly the rows the reader is looking at.
  let lastQuery = null;

  function renderOptions(filterText = "") {
    const q = filterText.trim().toLowerCase();
    const freshQuery = q !== lastQuery;
    lastQuery = q;
    const blocks = [];
    for (const c of countries) {
      const cities = c.cities || [];
      const countryHit = !q || c.label.toLowerCase().includes(q);
      const cityHits = q ? cities.filter((t) => t.label.toLowerCase().includes(q)) : cities;
      if (q && !countryHit && !cityHits.length) continue;
      // A city that matched what somebody typed but sits inside a
      // collapsed country reads as no match at all, so open it.
      if (freshQuery && q && cityHits.length) expanded.add(c.value);
      const isOpen = expanded.has(c.value);
      // When the query itself picked cities out, show those and not the
      // country's other forty.
      const shownCities = q && cityHits.length ? cityHits : cities;
      const buriedPicks = !isOpen && cities.some((t) => selectedCities.has(t.value));
      blocks.push(`
        <div class="ms-group">
          <div class="ms-group-row">
            ${optionHtml("country", c, selectedCountries.has(c.value))}
            ${
              cities.length
                ? `<button type="button" class="ms-chevron${buriedPicks ? " has-picks" : ""}"
                     data-country="${escapeHtml(c.value)}" aria-expanded="${isOpen}"
                     aria-label="${isOpen ? "Hide" : "Show"} cities in ${escapeHtml(c.label)}">${MS_CHEVRON_SVG}</button>`
                : ""
            }
          </div>
          ${
            isOpen && shownCities.length
              ? `<div class="ms-cities">${shownCities
                  .map((t) => optionHtml("city", t, selectedCities.has(t.value)))
                  .join("")}</div>`
              : ""
          }
        </div>`);
    }
    optionsEl.innerHTML = blocks.join("") || '<div class="ms-empty">No matches.</div>';
  }

  function emit() {
    onChange({ countries: [...selectedCountries], cities: [...selectedCities] });
  }

  function labelFor(value, kind) {
    if (kind === "country") {
      const hit = countries.find((c) => c.value === value);
      return hit ? hit.label : value;
    }
    for (const c of countries) {
      const hit = (c.cities || []).find((t) => t.value === value);
      if (hit) return hit.label;
    }
    return value;
  }

  function updateLabel() {
    const total = selectedCountries.size + selectedCities.size;
    if (total === 0) {
      toggle.textContent = placeholder;
    } else if (total === 1) {
      toggle.textContent = selectedCountries.size
        ? labelFor([...selectedCountries][0], "country")
        : labelFor([...selectedCities][0], "city");
    } else {
      toggle.textContent = `${total} selected`;
    }
    toggle.classList.toggle("active", total > 0);
  }

  function close() {
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    OPEN_MULTISELECTS.delete(close);
  }

  function open() {
    OPEN_MULTISELECTS.forEach((closeOther) => closeOther());
    menu.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    OPEN_MULTISELECTS.add(close);
    searchEl.focus();
  }

  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.hidden ? open() : close();
  });
  menu.addEventListener("click", (e) => e.stopPropagation()); // clicks inside the menu shouldn't bubble to document and self-close it

  optionsEl.addEventListener("change", (e) => {
    if (!e.target.matches('input[type="checkbox"]')) return;
    const set = e.target.dataset.kind === "city" ? selectedCities : selectedCountries;
    e.target.checked ? set.add(e.target.value) : set.delete(e.target.value);
    updateLabel();
    emit();
  });

  // The chevron sits outside the <label> on purpose: a click anywhere
  // inside a label toggles its checkbox, and expanding a country is not
  // the same act as filtering by it.
  optionsEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".ms-chevron");
    if (!btn) return;
    const value = btn.dataset.country;
    expanded.has(value) ? expanded.delete(value) : expanded.add(value);
    renderOptions(searchEl.value);
  });

  searchEl.addEventListener("input", () => renderOptions(searchEl.value));

  container.querySelector(".ms-clear").addEventListener("click", () => {
    selectedCountries.clear();
    selectedCities.clear();
    renderOptions(searchEl.value);
    updateLabel();
    emit();
  });

  renderOptions();
  updateLabel();

  return {
    setOptions(next) {
      // An empty list is what a facets payload still in the old shape
      // degrades to (see normalizeLocationFacets). Reconciling against
      // it would drop every pick the reader can still read in the URL,
      // so treat it as no news and keep what is already on screen.
      if (!next.length) return;
      const before = selectedCountries.size + selectedCities.size;
      countries = next;
      const cityValues = new Set(next.flatMap((c) => (c.cities || []).map((t) => t.value)));
      // Drop any pick the new option set no longer offers, same as the
      // flat widget does, and for the same reason: state must not keep
      // sending a value this dropdown no longer shows as selected.
      for (const v of [...selectedCountries]) {
        if (!next.some((c) => c.value === v)) selectedCountries.delete(v);
      }
      for (const v of [...selectedCities]) {
        if (!cityValues.has(v)) selectedCities.delete(v);
      }
      renderOptions(searchEl.value);
      updateLabel();
      if (selectedCountries.size + selectedCities.size !== before) emit();
    },
    reset() {
      selectedCountries.clear();
      selectedCities.clear();
      expanded.clear();
      searchEl.value = "";
      lastQuery = null;
      renderOptions("");
      updateLabel();
    },
    setSelected(countryValues, cityValues) {
      selectedCountries.clear();
      selectedCities.clear();
      countryValues.forEach((v) => selectedCountries.add(v));
      cityValues.forEach((v) => selectedCities.add(v));
      renderOptions(searchEl.value);
      updateLabel();
    },
  };
}

document.addEventListener("click", () => OPEN_MULTISELECTS.forEach((closeOther) => closeOther()));

// filter wiring

function wireFilters() {
  document.getElementById("f-search").addEventListener(
    "input",
    debounce((e) => {
      state.search = e.target.value.trim();
      state.offset = 0;
      loadJobs();
      loadTicker();
    }, 300)
  );

  msDepartment = createMultiSelect("ms-department", {
    placeholder: "Categories",
    onChange: (values) => {
      state.department = values;
      state.offset = 0;
      loadJobs();
      loadTicker();
    },
  });

  msSeniority = createMultiSelect("ms-seniority", {
    placeholder: "Levels",
    options: Object.entries(SENIORITY_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => {
      state.seniority = values;
      state.offset = 0;
      loadJobs();
      loadTicker();
    },
  });

  msCompany = createMultiSelect("ms-company", {
    placeholder: "Companies",
    searchable: true,
    onChange: (values) => {
      state.company = values;
      state.offset = 0;
      loadJobs();
      loadTicker();
    },
  });

  // One dropdown for both halves of where. Two adjacent location filters
  // made the reader decide which of them their question belonged in
  // before they could ask it, and the honest answer was often both.
  // Always searchable: the facet runs to a few hundred countries and
  // several thousand cities, and the place you want is one you already
  // have in mind, so typing beats scrolling.
  msLocation = createLocationSelect("ms-location", {
    placeholder: "Locations",
    onChange: ({ countries, cities }) => {
      state.country = countries;
      state.city = cities;
      state.offset = 0;
      loadJobs();
      loadTicker();
    },
  });

  msWorkplace = createMultiSelect("ms-workplace", {
    placeholder: "Workplace",
    options: Object.entries(WORKPLACE_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => {
      state.workplace = values;
      state.offset = 0;
      loadJobs();
      loadTicker();
    },
  });

  document.getElementById("f-date-posted").addEventListener("change", (e) => {
    if (!e.target.value) return; // the blank "Date posted" placeholder, not a real choice
    state.max_age_days = e.target.value === "any" ? "" : e.target.value;
    state.offset = 0;
    loadJobs();
    loadTicker();
  });

  // Newest/Oldest are both just the existing age-column sort under a
  // clearer label -- the th[data-sort="age"] click handler below does the
  // same thing, this is a second, more discoverable way in. Salary and
  // Relevance sorting aren't here: neither has real backing yet (salary
  // estimates are computed but never persisted as a sortable number, and
  // there's no ranking/search-relevance signal in the schema at all).
  document.getElementById("f-sort").addEventListener("change", (e) => {
    if (!e.target.value) return; // the blank "Sort" placeholder, not a real choice
    const [key, dir] = e.target.value.split(":");
    setActiveSortHeader(key, dir);
    state.offset = 0;
    loadJobs();
  });

  document.getElementById("view-switch").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-view]");
    if (btn) setView(btn.dataset.view);
  });

  document.getElementById("f-reset").addEventListener("click", () => {
    state.search = "";
    state.department = [];
    state.seniority = [];
    state.company = [];
    state.country = [];
    state.city = [];
    state.workplace = [];
    state.skills = [];
    state.max_age_days = "";
    state.starred_only = false;
    state.sort = "age";
    state.dir = "asc";
    state.offset = 0;
    document.getElementById("f-search").value = "";
    document.getElementById("f-date-posted").value = "";
    msDepartment.reset();
    msSeniority.reset();
    msCompany.reset();
    msLocation.reset();
    msWorkplace.reset();
    setActiveSortHeader("age", "asc");
    loadJobs();
    loadTicker();
  });

  document.getElementById("company-chip").addEventListener("click", () => {
    state.company = [];
    msCompany.reset();
    state.offset = 0;
    loadJobs();
    loadTicker();
  });

  document.getElementById("match-panel").addEventListener("click", (e) => {
    const chip = e.target.closest(".match-skill");
    if (!chip) return;
    state.skills = state.skills.filter((s) => s !== chip.dataset.matchSkill);
    if (!state.skills.length && state.sort === "match") setActiveSortHeader("age", "asc");
    state.offset = 0;
    loadJobs();
    loadTicker();
  });

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      const isSameColumn = state.sort === key;
      const dir = isSameColumn && state.dir === "desc" ? "asc" : "desc";
      setActiveSortHeader(key, dir);
      state.offset = 0;
      loadJobs();
    });
  });

  document.getElementById("filters-toggle").addEventListener("click", () => {
    const sub = document.getElementById("filters-sub");
    const open = sub.classList.toggle("open");
    document.getElementById("filters-toggle").setAttribute("aria-expanded", String(open));
  });
}

// Only meaningful below the @container breakpoint that collapses
// .filters-sub in the first place (see style.css) -- harmless to call
// unconditionally above it too, the button just stays display:none.
// Counts against #f-search deliberately excluded: it's always visible on
// its own, never one of the controls this button is hiding.
function updateFiltersToggleLabel() {
  let n = 0;

  if (state.department.length) n++;
  if (state.seniority.length) n++;
  if (state.company.length) n++;
  if (state.country.length) n++;
  if (state.city.length) n++;
  if (state.workplace.length) n++;
  if (state.max_age_days) n++;
  if (state.sort !== "age" || state.dir !== "asc") n++;
  document.getElementById("filters-toggle").textContent = n ? `Filters (${n})` : "Filters";
}

function setActiveSortHeader(key, dir) {
  state.sort = key;
  state.dir = dir;
  document.querySelectorAll("th[data-sort]").forEach((h) => {
    h.classList.remove("active");
    h.removeAttribute("data-dir");
  });
  // No header for this key: the board sorts by two columns and anything
  // else is a stale link or a typo. Threw a TypeError here before,
  // after the rows had already rendered, so the board looked fine while
  // the sort UI was left with nothing marked active.
  const th = document.querySelector(`th[data-sort="${key}"]`);
  if (th) {
    th.classList.add("active");
    th.setAttribute("data-dir", dir === "desc" ? "↓" : "↑");
  }
  // Keep the "Sort:" dropdown in step -- it only offers the two age-column
  // sorts (Newest/Oldest), so clicking the Age header updates it and
  // clicking the Listing header falls back to its blank "Sort" placeholder
  // rather than showing a now-wrong stale option.
  document.getElementById("f-sort").value = key === "age" ? `age:${dir}` : "";
}

// boot

// Board-scoped (see refreshFacetOptions) -- narrows with the board's own
// active filters, same as msDepartment/msLocation. NOT what the
// alert-creation form uses (see globalCompanyOptions below): an alert
// you're setting up for the future shouldn't be limited to whatever
// company filter happens to be active on the board right now.
let latestCompanyOptions = null;

// The alert-creation form's own company list: every resolved company,
// unscoped, fetched once and reused (populateAlertFilterOptions runs on
// every refreshStats() tick, but there's no reason to re-fetch the same
// global list that often -- companies.length changes maybe once a day).
let globalCompanyOptions = null;
async function loadGlobalCompanyOptions() {
  if (globalCompanyOptions) return globalCompanyOptions;
  try {
    const companies = await getJSON("/companies?resolved_only=1");
    globalCompanyOptions = [...companies.companies]
      .sort((a, b) => a.domain.localeCompare(b.domain))
      .map((c) => ({ value: c.domain, label: c.domain }));
  } catch {
    // Non-fatal: the alert form's company picker just stays empty/stale.
  }
  return globalCompanyOptions;
}

// Category/Location/Company dropdown counts, scoped to whatever ELSE is
// currently selected -- reported live: picking Security then Israel-only
// still showed Security's GLOBAL count (369) in the dropdown, not the 98
// that combination actually returns, because these used to come from
// /api/stats' top_departments/top_locations, which are deliberately
// global (that's right for the Market Stats dashboard and the
// alert-creation form -- see populateAlertFilterOptions, which still
// reads latestStats directly for exactly that reason -- but wrong for a
// live filter's own option counts). /api/facets computes each facet
// with every OTHER active filter applied but that facet's own key
// excluded (server-side, see handler.py's route_facets), so this is one
// query per call, not a client-side recount. Called from loadJobs() on
// every filter change, and from refreshStats()'s periodic tick so counts
// still track new postings without waiting for the next filter click.
// The locations facet, sanity-checked before it reaches the dropdown.
//
// Two different files answer that key. The filtered path asks the API,
// which is being deployed with the nested country/cities shape. The
// unfiltered path (every page load, every timer tick) reads the
// precomputed /facets.json, which the merge only rewrites on its next
// run, so for one cycle after deploy it is still the old flat list of
// raw location strings. Those entries have no `label` and no `cities`,
// so rendering them as countries would put "Tel Aviv, Israel" at the top
// level and then send it to the API as country=Tel Aviv, Israel.
//
// The `label` is what tells the two shapes apart: the new contract is
// the only one that carries a country name separate from its code.
// Anything else degrades to an empty list, which setOptions treats as
// no news rather than as a reason to clear the reader's picks. Nothing
// in here can throw either -- refreshFacetOptions wraps every facet in
// one try/catch, so a single bad key takes the categories and companies
// dropdowns down with it.
// Fills COUNTRY_LABELS_SEEN as a side effect, so describeAlertFilter can
// name a code without the frontend carrying its own country list.
function normalizeLocationFacets(rows) {
  if (!Array.isArray(rows)) return [];
  return rows
    .filter((r) => r && typeof r.value === "string" && typeof r.label === "string")
    .map((r) => {
      COUNTRY_LABELS_SEEN.set(r.value, r.label);
      return {
        value: r.value,
        label: r.label,
        n: r.n,
        cities: (Array.isArray(r.cities) ? r.cities : [])
          .filter((c) => c && typeof c.value === "string")
          .map((c) => ({ value: c.value, label: c.value, n: c.n })),
      };
    });
}

let facetsRequestSeq = 0;

async function refreshFacetOptions() {
  const seq = ++facetsRequestSeq;
  try {
    // Only the unfiltered case has a static answer, which is also the
    // one every page load and every timer tick asks for. Any active
    // filter goes to the API, where the counts are scoped to it.
    //
    // confidence is the exception: the board always sends one, so it is
    // not a filter in the sense that matters here. The static file holds
    // a variant per value.
    const active = { ...currentFilterParams(), confidence: "" };
    const facets = qs(active)
      ? await getJSON(`/facets?${qs(currentFilterParams())}`)
      : await getStaticFacets(state.confidence || "verified");
    if (seq !== facetsRequestSeq) return; // a newer filter is already being counted
    msDepartment.setOptions(facets.categories.map((r) => ({ value: r.value, label: `${r.value} (${r.n})` })));
    msLocation.setOptions(normalizeLocationFacets(facets.locations));
    // Alphabetical, not by count: this list is searchable/typed-into, not
    // browsed top-down like Category/Location, so a stable, scannable
    // order matters more here than leading with the biggest hirers.
    latestCompanyOptions = [...facets.companies]
      .sort((a, b) => a.value.localeCompare(b.value))
      .map((r) => ({ value: r.value, label: `${r.value} (${r.n})` }));
    msCompany.setOptions(latestCompanyOptions);
    populateAlertFilterOptions();
  } catch {
    // Non-fatal: worst case the dropdowns keep their previous option set.
  }
}

// theme (light/dark)

const THEME_KEY = "iljobs_theme";

function wireThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  const sync = () => {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    btn.textContent = isDark ? "Light" : "Dark";
  };
  sync(); // index.html's inline head script already applied the saved theme before this ran

  function swapTheme() {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    if (next === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // Private browsing or a full quota. The theme still switches; it
      // just will not be remembered, which is not worth failing over.
    }
    sync();
  }

  // One crossfade of the whole page, run by the compositor, instead of a
  // transition on every element.
  //
  // The old version put `transition: background-color, color,
  // border-color, fill` on .theme-transition * !important for 450ms.
  // That is 2,430 elements on a full board, four properties each: around
  // 9,700 transitions, none of which the compositor can help with,
  // because changing a colour means repainting. Hence the lag on a
  // phone, reported live.
  //
  // And it never animated anything anyway. Measured with a
  // transitionstart counter on a real page: zero started, and the colours
  // were at their new values in the same frame as the click. The class
  // and the theme attribute were set in one go, so no style was ever
  // resolved with the transition in place and nothing had a value to
  // animate from.
  //
  // A view transition has neither problem. The browser snapshots the
  // page before and after and crossfades the two images on the
  // compositor: one animation, no repaint per frame, and it cannot fail
  // to start because the snapshot is taken before the callback runs.
  btn.addEventListener("click", () => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // Firefox has no view transitions yet. There the theme just snaps,
    // which is what it already did on every browser.
    if (reduced || typeof document.startViewTransition !== "function") {
      swapTheme();
      return;
    }
    document.startViewTransition(swapTheme);
  });
}

// Topbar ticker: 10 most recent listings matching the board's current
// filters, not a fixed sitewide list. Called from every filter-changing
// handler, but not pagination/sort (those don't change what "recent"
// means). Duplicated once in the DOM so the CSS marquee loops seamlessly.
let tickerRequestSeq = 0;
let tickerInFlight = null;

async function loadTicker() {
  const seq = ++tickerRequestSeq;
  if (tickerInFlight) tickerInFlight.abort();
  const inFlight = new AbortController();
  tickerInFlight = inFlight;
  setLoadBar(true);
  try {
    return await _loadTicker(seq, inFlight.signal);
  } finally {
    setLoadBar(false); // see the note in loadJobs: never gate this
  }
}

// Same sequencing as loadJobs, and for the same reason: this rides the
// board's own filters, so without it the strip can end up showing the
// ten newest for a filter nobody is looking at any more.
async function _loadTicker(seq, signal) {
  const track = document.getElementById("ticker-track");
  try {
    const params = qs({ ...currentFilterParams(), limit: 10, sort: "age", dir: "asc" });
    const data = await getJSON(`/jobs?${params}`, { signal });
    if (seq !== tickerRequestSeq) return;
    if (!data.jobs.length) {
      track.innerHTML = "";
      return;
    }
    const itemsHtml = data.jobs
      .map(
        (j) => `
        <a class="ticker-item" href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener">
          <span class="bullet">●</span>${escapeHtml(j.title)}
          <span class="ticker-company">@${escapeHtml(companyLabel(j))}</span>
        </a>`
      )
      .join("");
    track.innerHTML = itemsHtml + itemsHtml;
    // Roughly constant per-item reading speed regardless of list length,
    // rather than a fixed duration that'd crawl for 3 items and race for 10.
    // 7s an item, 70s for the usual 10. Was 4s, which pulled the eye.
    track.style.animationDuration = `${data.jobs.length * 7}s`;
  } catch {
    // Non-fatal: purely decorative, the board itself doesn't depend on it.
  }
}

// Refreshes everything driven by /api/stats -- metrics, market panels,
// filter option counts. Not the job table/pagination itself: a listing
// re-rendering under a user mid-scroll or mid-page would be more
// disruptive than useful, so that stays a manual reload.
// Cached so the alert-creation form can reuse the board's own last-fetched
// stats (top_departments/top_locations) instead of a duplicate fetch.
let latestStats = null;

const STATS_CACHE_KEY = "iljobs_stats_cache";

function getCachedStats() {
  try {
    const raw = localStorage.getItem(STATS_CACHE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && parsed.totals ? parsed : null;
  } catch {
    return null;
  }
}

// The applier publishes /stats.json and /facets.json to the frontend
// bucket on every write cycle, so CloudFront can hand the browser the
// same answer the API would compute without invoking anything. Same
// pattern bootstrap.json has always used.
//
// This is where the API's compute bill actually was: the page polls
// these on a timer per open tab, and each poll was a Lambda cold start
// downloading a 308MB snapshot to hand back JSON already sitting in S3.
//
// The API route stays exactly as it was, both as the fallback here and
// as the answer anyone calling /api/stats directly still gets. A missing
// or unreachable static copy is a normal state, not an error: it is what
// every deploy looks like until the next merge runs.
// Facets come keyed by confidence, since the board always sends one.
// An older file without the variant map, or none at all, falls back to
// the API rather than guessing.
async function getStaticFacets(confidence) {
  try {
    const r = await fetch("/facets.json", { cache: "no-store" });
    if (r.ok) {
      const byConfidence = await r.json();
      if (byConfidence && byConfidence[confidence]) return byConfidence[confidence];
    }
  } catch {
    // Falls through to the API, which is the authority anyway.
  }
  return getJSON(`/facets?${qs(currentFilterParams())}`);
}

async function getStaticOrApi(staticPath, apiPath) {
  try {
    const r = await fetch(staticPath, { cache: "no-store" });
    if (r.ok) return await r.json();
  } catch {
    // Falls through to the API, which is the authority anyway.
  }
  return getJSON(apiPath);
}

async function refreshStats() {
  // Cache-first, matching the job list. Without this the Statistics
  // column sat on skeletons for the whole request on every single visit,
  // even though /api/stats changes at most once an hour (the merge) and
  // the previous answer is almost always still correct. The request
  // still always runs and silently replaces this.
  const cachedStats = getCachedStats();
  if (cachedStats) {
    latestStats = cachedStats;
    renderMetrics(cachedStats);
    renderPanels(cachedStats);
    renderScopedPanels(cachedStats);
  }

  setLoadBar(true);
  try {
    const stats = await getStaticOrApi("/stats.json", "/stats");
    latestStats = stats;
    renderMetrics(stats);
    renderPanels(stats);
    renderScopedPanels(stats);
    refreshFacetOptions();
    // Not awaited, and forced: a filtered board's tiles go stale on the
    // same 2-minute clock the global ones do, and this tick is also the
    // catch-up a tab gets when it becomes visible again.
    refreshScopedStats({ force: true });
    populateAlertFilterOptions();
    try {
      localStorage.setItem(STATS_CACHE_KEY, JSON.stringify(stats));
    } catch {
      // Quota or private browsing. Purely a speed-up, never load-bearing.
    }
  } catch (err) {
    // Don't bury a perfectly good cached dashboard under an error banner
    // over one failed refresh.
    if (!cachedStats) {
      const msg = `<div class="error-state" style="grid-column:1/-1">Could not load /api/stats: ${escapeHtml(err.message)}</div>`;
      document.getElementById("metrics-grid").innerHTML = msg;
      document.getElementById("panel-grid").innerHTML = msg;
      // The other two grids are the same failure, and four copies of one
      // message are not four times the information. Clear their bones so
      // nothing sits there pretending to still be loading.
      document.getElementById("scoped-panel-grid").innerHTML = "";
      document.getElementById("pipeline-grid").innerHTML = "";
    }
  } finally {
    setLoadBar(false);
  }
}

// The scoped half of the Statistics column: the five tiles and Top
// hirers, asked for with whatever the filter bar currently says.
//
// Sequenced the way loadJobs is, and for the same reason it had to be. A
// scoped /stats was measured at up to 2.9 seconds, which is several
// filter changes' worth of typing, and without the counter a slow answer
// for an abandoned filter paints straight over a fast one for the
// current filter. Abort rather than ignore, too: an answer nobody will
// read still costs the reader's bandwidth and us a Lambda invocation.
let scopedStatsSeq = 0;
let scopedStatsInFlight = null;
// The filter string currently on the wire, so loadJobs' calls for a page
// turn or a sort flip (neither of which changes what is counted) do not
// each fire a duplicate of the request already running.
let scopedStatsPending = null;

async function refreshScopedStats({ force = false } = {}) {
  // refreshFacetOptions' test, not a second definition of it: confidence
  // rides along on every request the board makes, so it is not a filter
  // in the sense that decides between the static artifact and the API.
  const params = qs({ ...currentFilterParams(), confidence: "" });

  if (!params) {
    // Unfiltered, which is the path that must stay on the precomputed
    // /stats.json artifact. refreshStats is already holding that answer,
    // so there is nothing to ask anyone.
    if (scopedStatsInFlight) scopedStatsInFlight.abort();
    scopedStatsInFlight = null;
    scopedStatsPending = null;
    scopedStatsSeq++; // retires anything still resolving
    latestScoped = null;
    renderScopeDependent();
    return;
  }

  const held = latestScoped !== null && latestScoped.params === params;
  if (!force && (held || scopedStatsPending === params)) {
    renderScopeDependent(); // already answered, or already being asked
    return;
  }

  const seq = ++scopedStatsSeq;
  if (scopedStatsInFlight) scopedStatsInFlight.abort();
  const inFlight = new AbortController();
  scopedStatsInFlight = inFlight;
  scopedStatsPending = params;
  // Only when the question actually changed. A forced refresh of the
  // same filters keeps its numbers on screen while it revalidates; a new
  // filter drops them, because the previous filter's count sitting under
  // a new search reads as the new search's count.
  if (!held) latestScoped = null;
  renderScopeDependent();

  setLoadBar(true);
  try {
    const data = await getJSON(`/stats?${qs(currentFilterParams())}`, { signal: inFlight.signal });
    if (seq !== scopedStatsSeq) return;
    const scoped = data && data.scoped;
    // No `scoped` key is a normal answer, not a broken one: it is what
    // the unfiltered route returns and what a stale artifact or a
    // pre-deploy API looks like. Fall back to whole-board numbers, under
    // whole-board labels, and let the scope line say what happened.
    latestScoped =
      scoped && typeof scoped.open_jobs === "number"
        ? { params, data: scopeShape(scoped) }
        : { params, data: null, degraded: true };
    renderScopeDependent();
  } catch (err) {
    if (seq !== scopedStatsSeq || err.name === "AbortError") return;
    // One failed sidebar refresh does not earn an error banner. Same
    // fallback as a payload with no `scoped` key.
    latestScoped = { params, data: null, degraded: true };
    renderScopeDependent();
  } finally {
    if (seq === scopedStatsSeq) scopedStatsPending = null;
    // Unconditional, unlike loadJobs': the bar is reference counted and
    // this call incremented it exactly once, so skipping the decrement
    // on a superseded request would leave it running forever.
    setLoadBar(false);
  }
}

// Everything in the sidebar that has to agree about the board's current
// filters, repainted together so the tiles, Top hirers and the scope
// line can never disagree about which question they are answering.
function renderScopeDependent() {
  if (!latestStats) {
    // No stats in hand yet, so the markup's own skeletons stay up. The
    // scope line still paints: it is built from state, not from a
    // response, and it is the one thing that can be right immediately.
    renderScopeLine();
    return;
  }
  renderScopedMetrics(latestStats); // paints the scope line with them
  renderScopedPanels(latestStats);
}

// /api/health is a tiny, cheap endpoint built for exactly this: a
// freshness ping without hauling the whole /stats payload over again.
// Polled far more often than the 2-min full stats refresh so the "X AGO"
// text and the sync countdown don't sit on data up to a full 2-min-poll
// -plus-2-min-CDN-cache stale (reported live: the countdown was landing
// a few minutes off). Doesn't touch latestStats or re-render panels --
// setLastCheckedAt's monotonic guard means this can only ever pull the
// countdown's anchor forward, never regress it against a fresher value
// the last /stats poll already saw.
// Was 30s, which predates the 5-minute write cycle: it asked the same
// question ten times per available answer, and each miss past
// CloudFront's window was a Lambda invocation. 120s still sees every
// cycle twice.
const HEALTH_POLL_MS = 120_000;

async function refreshFreshness() {
  try {
    const health = await getJSON("/health");
    setLastCheckedAt(health.last_checked);
    tickApiStatus();
  } catch {
    // Non-fatal: the next /stats or /health poll will catch up.
  }
}

// Reported live: the "next sync" line just said "syncing" once the
// countdown hit zero, with no way to tell whether anything was actually
// happening versus a missed/failed cycle. Read from /api/pipeline-status
// (api/handler.py's route_pipeline_status), which now reports BOTH
// halves of the pipeline separately -- {scrape: {...}, merge: {...}} --
// since they run on genuinely different schedules (fast-poll/workday
// every 5-10 min, the merge every 5). pipelineActivityText() above reads
// .merge: see its own comment for why the scrape half, active almost
// continuously, wouldn't actually answer "when does the site next
// update."
let pipelinePhase = null;

async function refreshPipelineStatus() {
  try {
    pipelinePhase = await getJSON("/pipeline-status");
    tickApiStatus();
  } catch {
    // Non-fatal: the activity line simply stays empty when this is null.
  }
}

// Auth. Three passwordless sign-in paths, no accounts endpoint on
// this API beyond what a Cognito JWT authorizer will eventually protect
// (/me/alerts). See infra/cognito.tf and github_auth_handler.py for the
// backend half of each of these.

const COGNITO_DOMAIN = "iljobs-auth-876913698688.auth.il-central-1.amazoncognito.com";
const COGNITO_REGION = "il-central-1";
const COGNITO_CLIENT_ID = "5021pv23cp3udp1uaq34tp38mb";
// OAuth client IDs aren't secret, safe to ship in frontend JS same as
// Google's. The paired client secret is NOT here and never should be:
// it lives only in the github-auth Lambda's environment, set from
// var.github_oauth_client_secret (infra/github_auth_lambda.tf), because
// only the server side of the code exchange is allowed to hold it.
const GITHUB_OAUTH_CLIENT_ID = "Ov23lii8kIqDUL9aLhxh";
// Flips to true once infra/cognito.tf's aws_cognito_identity_provider.google
// actually exists (real Google Cloud Console credentials set). Until then,
// redirecting to Cognito's /oauth2/authorize?identity_provider=Google lands
// on Cognito's own generic "Login option is not available" hosted-UI error
// page instead -- confusing, and a full navigation away from this app's own
// error display. Guarded the same way GitHub is instead.
const GOOGLE_CONFIGURED = false;
const AUTH_TOKENS_KEY = "iljobs_auth_tokens";
const PKCE_VERIFIER_KEY = "iljobs_pkce_verifier"; // sessionStorage: only needs to survive the redirect round-trip

function getAuthTokens() {
  try {
    return JSON.parse(localStorage.getItem(AUTH_TOKENS_KEY) || "null");
  } catch {
    return null;
  }
}

function setAuthTokens(tokens) {
  localStorage.setItem(AUTH_TOKENS_KEY, JSON.stringify(tokens));
}

function signOut() {
  localStorage.removeItem(AUTH_TOKENS_KEY);
  renderAuthState();
}

// No verification -- this is display-only (the signed-in email in the
// topbar). The one place a token's signature actually has to hold up is
// server-side, when a JWT authorizer validates it on /me/alerts.
function decodeJwtEmail(idToken) {
  try {
    const payload = JSON.parse(atob(idToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.email || null;
  } catch {
    return null;
  }
}

async function base64UrlDigest(input) {
  const bytes = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function randomUrlSafe(len) {
  const bytes = crypto.getRandomValues(new Uint8Array(len));
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function startGoogleSignIn() {
  if (!GOOGLE_CONFIGURED) {
    showAuthError("Google sign-in isn't wired up yet.");
    return;
  }
  const verifier = randomUrlSafe(64);
  sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
  const challenge = await base64UrlDigest(verifier);
  const redirectUri = `${location.origin}/`;
  const url = `https://${COGNITO_DOMAIN}/oauth2/authorize?${qs({
    client_id: COGNITO_CLIENT_ID,
    response_type: "code",
    scope: "openid email profile",
    redirect_uri: redirectUri,
    identity_provider: "Google",
    code_challenge: challenge,
    code_challenge_method: "S256",
  })}`;
  location.href = url;
}

function startGithubSignIn() {
  if (!GITHUB_OAUTH_CLIENT_ID) {
    showAuthError("GitHub sign-in isn't wired up yet.");
    return;
  }
  const url = `https://github.com/login/oauth/authorize?${qs({
    client_id: GITHUB_OAUTH_CLIENT_ID,
    redirect_uri: `${location.origin}/api/auth/github/callback`,
    scope: "read:user user:email",
  })}`;
  location.href = url;
}

// Cognito's InitiateAuth/RespondToAuthChallenge are deliberately public,
// unsigned operations for a user-pool app client -- callable directly
// from the browser, no backend proxy or AWS SDK needed for this part.
async function cognitoRequest(target, body) {
  const res = await fetch(`https://cognito-idp.${COGNITO_REGION}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.message || data.__type || `HTTP ${res.status}`);
  return data;
}

let _pendingOtpEmail = null;
let _pendingOtpSession = null;

async function startEmailSignIn(email) {
  // Ensures the Cognito account row exists first -- required because
  // allow_admin_create_user_only=true also blocks Cognito's own public
  // SignUp API, see github_auth_handler.py's module docstring.
  const res = await fetch(`${API_BASE}/auth/email/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const init = await cognitoRequest("InitiateAuth", {
    ClientId: COGNITO_CLIENT_ID,
    AuthFlow: "USER_AUTH",
    AuthParameters: { USERNAME: email, PREFERRED_CHALLENGE: "EMAIL_OTP" },
  });
  _pendingOtpEmail = email;
  _pendingOtpSession = init.Session;
}

async function verifyEmailOtp(code) {
  const result = await cognitoRequest("RespondToAuthChallenge", {
    ClientId: COGNITO_CLIENT_ID,
    ChallengeName: "EMAIL_OTP",
    Session: _pendingOtpSession,
    ChallengeResponses: { USERNAME: _pendingOtpEmail, EMAIL_OTP_CODE: code },
  });
  const t = result.AuthenticationResult;
  setAuthTokens({ id_token: t.IdToken, access_token: t.AccessToken, refresh_token: t.RefreshToken });
  _pendingOtpEmail = null;
  _pendingOtpSession = null;
}

async function exchangeGoogleCode(code) {
  const verifier = sessionStorage.getItem(PKCE_VERIFIER_KEY);
  sessionStorage.removeItem(PKCE_VERIFIER_KEY);
  const res = await fetch(`https://${COGNITO_DOMAIN}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: qs({
      grant_type: "authorization_code",
      client_id: COGNITO_CLIENT_ID,
      code,
      redirect_uri: `${location.origin}/`,
      code_verifier: verifier,
    }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error_description || data.error || `HTTP ${res.status}`);
  setAuthTokens({ id_token: data.id_token, access_token: data.access_token, refresh_token: data.refresh_token });
}

// Two unrelated redirect shapes land here, both back at "/": Google's
// via Cognito's own authorization-code flow (?code=... query param,
// exchanged client-side above) and GitHub's via github_auth_handler.py's
// own 302 (#id_token=...&access_token=...&refresh_token=... hash
// fragment -- that Lambda already did the full exchange server-side).
async function handleAuthRedirect() {
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get("id_token")) {
    setAuthTokens({
      id_token: hash.get("id_token"),
      access_token: hash.get("access_token"),
      refresh_token: hash.get("refresh_token"),
    });
    history.replaceState(null, "", location.pathname + location.search);
    return;
  }
  if (hash.get("auth_error")) {
    console.error("Sign-in failed:", hash.get("auth_error"));
    history.replaceState(null, "", location.pathname + location.search);
    return;
  }

  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  if (code) {
    history.replaceState(null, "", location.pathname); // strip ?code= before the async exchange, not after -- a reload mid-flight must not resubmit a single-use code
    try {
      await exchangeGoogleCode(code);
    } catch (err) {
      console.error("Google sign-in failed:", err);
    }
  }
}

function showAuthError(msg) {
  const el = document.getElementById("auth-error");
  el.textContent = msg;
  el.hidden = false;
}

// Reported live as "OTP isn't sending" -- it was sending fine, but a
// stale error from an earlier attempt (e.g. clicking GitHub first) never
// got cleared, so it sat on screen looking like the *current* action had
// just failed. Called at the start of every provider/form action below.
function clearAuthError() {
  document.getElementById("auth-error").hidden = true;
}

function renderAuthState() {
  const area = document.getElementById("auth-area");
  // Pages other than the board load this file for authedFetch and the
  // token helpers but have no topbar auth slot to paint into. The
  // account page cannot simply grow one either: this panel carries the
  // alert form's ids, and that page already has them.
  if (!area) return;
  const tokens = getAuthTokens();
  if (!tokens) {
    area.innerHTML = `
      <button class="auth-trigger" id="auth-trigger" type="button">Sign In</button>
      <div class="auth-panel" id="auth-panel" hidden>
        <button class="auth-provider-btn" id="auth-google" type="button">
          <svg viewBox="0 0 18 18" width="16" height="16" aria-hidden="true"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z"/><path fill="#FBBC05" d="M3.964 10.71c-.18-.54-.282-1.117-.282-1.71s.102-1.17.282-1.71V4.958H.957C.348 6.173 0 7.548 0 9s.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/></svg>
          <span>Continue with Google</span>
        </button>
        <button class="auth-provider-btn" id="auth-github" type="button">
          <svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
          <span>Continue with GitHub</span>
        </button>
        <div class="auth-divider">or</div>
        <form class="auth-email-form" id="auth-email-form">
          <input type="email" id="auth-email-input" placeholder="you@example.com" required autocomplete="email" />
          <button class="btn" type="submit">Send code</button>
        </form>
        <form class="auth-email-form" id="auth-otp-form" hidden>
          <input type="text" id="auth-otp-input" placeholder="Verification code" inputmode="numeric" pattern="[0-9]{4,10}" required autocomplete="one-time-code" />
          <button class="btn" type="submit">Verify</button>
        </form>
        <p class="auth-error" id="auth-error" hidden></p>
      </div>`;
    wireAuthTrigger();
    wireAuthPanel();
    return;
  }
  const email = decodeJwtEmail(tokens.id_token) || "signed in";
  // An icon, not the address. The topbar is the one place on the site a
  // reader's own email was on screen permanently, including over a
  // shoulder and in any screenshot they take of the board. The address
  // is still there for anyone who wants it, as the button's title and
  // its accessible name, which is also where a screen reader reads it.
  area.innerHTML = `
    <button class="auth-trigger" id="topbar-alert-btn" type="button">+ Alert</button>
    <button class="auth-trigger auth-account" id="auth-trigger" type="button"
            title="${escapeHtml(email)}" aria-label="Account, signed in as ${escapeHtml(email)}">
      <svg class="account-icon" aria-hidden="true"><use href="#account"></use></svg>
    </button>
    <div class="auth-panel alerts-panel" id="auth-panel" hidden>
      <div class="alerts-header alerts-header-row">
        <span>My Alerts</span>
        <a class="link account-link" href="/account">Account</a>
      </div>
      <div id="alerts-list"><p class="alerts-empty">Loading…</p></div>

      <div class="alert-create" id="alert-create">
        <div class="alerts-header" id="alert-form-title">New Alert</div>
        <div class="alert-create-fields">
          <input type="text" id="alert-f-search" placeholder="SEARCH" />
          <div class="ms" id="alert-ms-department"></div>
          <div class="ms" id="alert-ms-seniority"></div>
          <div class="ms" id="alert-ms-company"></div>
          <div class="ms" id="alert-ms-location"></div>
          <div class="ms" id="alert-ms-workplace"></div>
        </div>
        <div class="alert-form-actions">
          <button class="btn" id="create-alert-btn" type="button">Create Alert</button>
          <button class="btn btn-quiet" id="cancel-edit-btn" type="button" hidden>Cancel</button>
        </div>
        <p class="create-alert-feedback" id="create-alert-feedback" hidden></p>
      </div>

      <button class="auth-signout" id="auth-signout" type="button">Sign Out</button>
    </div>`;
  wireAuthTrigger();
  wireTopbarAlertButton();
  document.getElementById("auth-signout").addEventListener("click", signOut);
  wireAlertCreateForm();
  loadMyAlerts().then(renderAlertsList);
}

function wireAuthTrigger() {
  const trigger = document.getElementById("auth-trigger");
  const panel = document.getElementById("auth-panel");
  trigger.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    trigger.classList.toggle("active", !panel.hidden);
  });
}

// A second trigger next to the email one, signed-in only -- opens the
// same panel (My Alerts + New Alert form), just a more discoverable
// entry point than clicking your own email. Doesn't create anything
// itself. Rewired on every renderAuthState() re-render like the rest of
// this panel's internals, since sign-in/out replaces the whole subtree.
function wireTopbarAlertButton() {
  document.getElementById("topbar-alert-btn").addEventListener("click", () => {
    const trigger = document.getElementById("auth-trigger");
    const panel = document.getElementById("auth-panel");
    panel.hidden = false;
    trigger.classList.add("active");
  });
}

function wireAuthPanel() {
  document.getElementById("auth-google").addEventListener("click", () => { clearAuthError(); startGoogleSignIn(); });
  document.getElementById("auth-github").addEventListener("click", () => { clearAuthError(); startGithubSignIn(); });

  document.getElementById("auth-email-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearAuthError();
    const email = document.getElementById("auth-email-input").value.trim();
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      await startEmailSignIn(email);
      document.getElementById("auth-email-form").hidden = true;
      document.getElementById("auth-otp-form").hidden = false;
      document.getElementById("auth-otp-input").focus();
    } catch (err) {
      showAuthError(err.message || "Could not send a code. Try again.");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("auth-otp-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearAuthError();
    const code = document.getElementById("auth-otp-input").value.trim();
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      await verifyEmailOtp(code);
      renderAuthState();
    } catch (err) {
      showAuthError(err.message === "CodeMismatchException" ? "Wrong code, try again." : err.message || "Could not verify that code.");
    } finally {
      btn.disabled = false;
    }
  });
}

function wireAuth() {
  renderAuthState();

  // Once, not inside wireAuthPanel (which reruns on every sign-in-state
  // re-render and was stacking up a fresh document-level listener each
  // time, each one closing over that render's now-detached panel/trigger
  // elements -- harmless-looking but wasteful). Looks up the current
  // panel live rather than closing over a specific render's elements.
  //
  // mousedown, not click: reported live -- drag-selecting the pasted OTP
  // code inside the input closed the panel mid-selection. A selection
  // drag's mouseup (and the click it generates) can land outside the
  // input if the drag overshoots, so a click-based outside-check treats
  // a normal text selection as a dismiss. mousedown fires on press,
  // before any drag happens, so it isn't fooled by where the drag ends.
  document.addEventListener("mousedown", (e) => {
    const panel = document.getElementById("auth-panel");
    const trigger = document.getElementById("auth-trigger");
    if (panel && !panel.hidden && !e.target.closest("#auth-area")) {
      panel.hidden = true;
      trigger.classList.remove("active");
    }
  });
}

// /me/alerts: the one authenticated area of the app. authedFetch()
// refreshes the id_token first if it's about to expire (Cognito's
// public, unsigned REFRESH_TOKEN_AUTH -- same pattern as the other
// direct cognitoRequest() calls), so a signed-in tab doesn't silently
// start failing an hour in.

async function ensureFreshTokens() {
  const tokens = getAuthTokens();
  if (!tokens) return null;
  const payload = JSON.parse(atob(tokens.id_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
  if (payload.exp * 1000 - Date.now() > 60_000) return tokens; // still good for at least another minute
  try {
    const result = await cognitoRequest("InitiateAuth", {
      ClientId: COGNITO_CLIENT_ID,
      AuthFlow: "REFRESH_TOKEN_AUTH",
      AuthParameters: { REFRESH_TOKEN: tokens.refresh_token },
    });
    const t = result.AuthenticationResult;
    const refreshed = { id_token: t.IdToken, access_token: t.AccessToken, refresh_token: tokens.refresh_token };
    setAuthTokens(refreshed);
    return refreshed;
  } catch {
    signOut();
    return null;
  }
}

async function authedFetch(path, options = {}) {
  const tokens = await ensureFreshTokens();
  if (!tokens) throw new Error("Signed out");
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}), Authorization: `Bearer ${tokens.id_token}` },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

async function loadMyAlerts() {
  try {
    const data = await authedFetch("/me/alerts");
    return data.alerts || [];
  } catch {
    return [];
  }
}

// Short, readable summary of a saved filter -- not every possible key,
// just the ones a user is likely to have actually set.
// A country code as the facets name it. The board never ships a country
// list of its own: the codes it shows came from /api/facets, and this
// remembers those labels so a saved alert reads "Israel" rather than
// "IL". Falls back to the code, which is what an alert saved against a
// country the facet no longer lists will show.
const COUNTRY_LABELS_SEEN = new Map();

function countryLabel(code) {
  return COUNTRY_LABELS_SEEN.get(code) || code;
}

// Trimmed, because a saved value like "Tel Aviv, Israel" splits into a
// second part with a leading space and rendered as a double one.
function csv(value) {
  return String(value).split(",").map((v) => v.trim()).filter(Boolean);
}

function describeAlertFilter(filter) {
  const parts = [];
  if (filter.search || filter.q) parts.push(`"${filter.search || filter.q}"`);
  if (filter.keywords) parts.push(filter.keywords.split(";").map((t) => t.trim()).filter(Boolean).join(", "));
  if (filter.department) parts.push(csv(filter.department).join(", "));
  if (filter.seniority) parts.push(csv(filter.seniority).join(", "));
  if (filter.company) parts.push(csv(filter.company).join(", "));
  if (filter.location) parts.push(csv(filter.location).join(", "));
  if (filter.workplace) parts.push(csv(filter.workplace).join(", "));
  // Place, as the form now saves it. A code is shown by the name the
  // facets gave it, so a saved alert reads Israel rather than IL.
  if (filter.country) parts.push(csv(filter.country).map(countryLabel).join(", "));
  if (filter.city) parts.push(csv(filter.city).join(", "));
  // And as it used to. These alerts still match, because the API still
  // answers both keys, so they are still described.
  if (filter.israel_only === "1") parts.push("Israel only");
  return parts.length ? parts.join(" · ") : "All jobs";
}

function renderAlertsList(alerts) {
  const container = document.getElementById("alerts-list");
  if (!container) return; // signed out (or panel re-rendered) before this resolved
  myAlerts = alerts;
  // An alert deleted from under an open edit leaves nothing to save to.
  if (editingAlertId && !alerts.some((a) => a.alert_id === editingAlertId)) resetAlertForm();
  if (!alerts.length) {
    container.innerHTML = `<p class="alerts-empty">No alerts yet. Pick some filters below and create one.</p>`;
    return;
  }
  container.innerHTML = alerts
    .map(
      (a) => `
      <div class="alert-row ${a.active ? "" : "paused"} ${a.alert_id === editingAlertId ? "editing" : ""}">
        <button type="button" class="alert-summary" data-id="${a.alert_id}" title="Edit this alert">${escapeHtml(describeAlertFilter(a.filter))}</button>
        <span class="alert-actions">
          <button class="alert-toggle" data-id="${a.alert_id}" data-active="${a.active}">${a.active ? "Pause" : "Resume"}</button>
          <button class="alert-delete" data-id="${a.alert_id}" aria-label="Delete alert" title="Delete alert">✕</button>
        </span>
      </div>`
    )
    .join("");

  container.querySelectorAll(".alert-summary").forEach((btn) => {
    btn.addEventListener("click", () => startEditAlert(btn.dataset.id));
  });
  container.querySelectorAll(".alert-toggle").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await authedFetch(`/me/alerts/${btn.dataset.id}`, {
          method: "PATCH",
          body: JSON.stringify({ active: btn.dataset.active !== "true" }),
        });
        renderAlertsList(await loadMyAlerts());
      } catch {
        btn.disabled = false;
      }
    });
  });
  container.querySelectorAll(".alert-delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await authedFetch(`/me/alerts/${btn.dataset.id}`, { method: "DELETE" });
        renderAlertsList(await loadMyAlerts());
      } catch {
        btn.disabled = false;
      }
    });
  });
}

function showCreateAlertFeedback(msg, isError) {
  const el = document.getElementById("create-alert-feedback");
  if (!el) return; // panel got torn down (sign out) while a request was in flight
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 3000);
}

// New-alert form, lives inside the My Alerts panel with its own filter
// pickers -- deliberately independent of `state` (the board's live
// filters). Reported live: tying "+ Alert" to whatever the board
// happened to be showing meant you had to first go set up the board
// exactly right, create the alert, then undo it to keep browsing.
// Wired fresh each time the signed-in panel renders (sign in/out
// recreates the underlying DOM), same as wireAuthPanel().
let alertMsDepartment, alertMsSeniority, alertMsCompany, alertMsLocation, alertMsWorkplace;

const alertFormState = {
  // Same single field as the board's box, so an alert matches what you
  // were looking at when you saved it. Alerts saved before this still
  // carry q and the API still answers it.
  search: "",
  department: [],
  seniority: [],
  company: [],
  country: [],
  city: [],
  workplace: [],
};

// The alert being edited, or null for "create a new one". The form below
// is the same set of controls either way, so this is the only thing that
// distinguishes the two modes.
let editingAlertId = null;

// The last list we rendered, so clicking a row can reload that alert's
// filter without asking the API for it again.
let myAlerts = [];

// Turns a saved filter back into the form's own state shape. The stored
// form is what /api/jobs takes, where the multi-value fields are
// comma-joined strings.
function fillAlertForm(filter) {
  const list = (v) => (v ? String(v).split(",").filter(Boolean) : []);
  alertFormState.search = filter.search || filter.q || "";
  alertFormState.department = list(filter.department);
  alertFormState.seniority = list(filter.seniority);
  alertFormState.company = list(filter.company);
  alertFormState.country = list(filter.country);
  alertFormState.city = list(filter.city);
  // An alert saved before the board had countries. israel_only maps
  // exactly onto country=IL now that the two return the same listings,
  // so it is translated rather than dropped; the old raw-string location
  // is not, because its values were never canonical names and any
  // mapping would be a guess. Editing such an alert loses only the part
  // this form can no longer express.
  if (filter.israel_only === "1" && !alertFormState.country.includes("IL")) {
    alertFormState.country = ["IL", ...alertFormState.country];
  }
  alertFormState.workplace = list(filter.workplace);

  document.getElementById("alert-f-search").value = alertFormState.search;
  alertMsDepartment.setSelected(alertFormState.department);
  alertMsSeniority.setSelected(alertFormState.seniority);
  alertMsCompany.setSelected(alertFormState.company);
  alertMsLocation.setSelected(alertFormState.country, alertFormState.city);
  alertMsWorkplace.setSelected(alertFormState.workplace);
}

// Both modes paint from here, so the heading, the button and the Cancel
// affordance can never disagree about which one we are in.
function paintAlertFormMode() {
  const title = document.getElementById("alert-form-title");
  const submit = document.getElementById("create-alert-btn");
  const cancel = document.getElementById("cancel-edit-btn");
  if (!title || !submit || !cancel) return;
  title.textContent = editingAlertId ? "Edit Alert" : "New Alert";
  submit.textContent = editingAlertId ? "Save Changes" : "Create Alert";
  cancel.hidden = !editingAlertId;
  document.getElementById("alert-create").classList.toggle("editing", !!editingAlertId);
}

function startEditAlert(alertId) {
  const alert = myAlerts.find((a) => a.alert_id === alertId);
  if (!alert) return;
  editingAlertId = alertId;
  fillAlertForm(alert.filter || {});
  paintAlertFormMode();
  renderAlertsList(myAlerts);
  document.getElementById("alert-create").scrollIntoView({ block: "nearest" });
}

function cancelEditAlert() {
  resetAlertForm();
  renderAlertsList(myAlerts);
}

function resetAlertForm() {
  alertFormState.search = "";
  alertFormState.department = [];
  alertFormState.seniority = [];
  alertFormState.company = [];
  alertFormState.country = [];
  alertFormState.city = [];
  alertFormState.workplace = [];
  document.getElementById("alert-f-search").value = "";
  alertMsDepartment.reset();
  alertMsSeniority.reset();
  alertMsCompany.reset();
  alertMsLocation.reset();
  alertMsWorkplace.reset();
  editingAlertId = null;
  paintAlertFormMode();
}

// Deliberately global, not board-scoped: an alert is a standing filter
// for FUTURE postings, not a snapshot of whatever the board's own
// filters happen to show right now, so this reads latestStats'
// unscoped top_departments/top_locations (same ones the Market Stats
// panel uses) and its own globalCompanyOptions, never
// latestCompanyOptions/msDepartment/msLocation's board-scoped values.
function populateAlertFilterOptions() {
  if (!alertMsDepartment) return; // signed out, or panel not built yet
  // Not while someone is editing. setOptions drops any selection missing
  // from the new list and reports it through onChange, which would
  // rewrite the form under the user mid-edit. These lists barely move in
  // the two minutes between polls, and the next save repaints anyway.
  if (editingAlertId) return;
  if (latestStats) {
    alertMsDepartment.setOptions(
      latestStats.top_departments.map((r) => ({ value: r.department, label: `${r.department} (${r.n})` }))
    );
  }
  // Locations come from the facets tree, not latestStats.top_locations.
  // Those are raw strings, which is what the board stopped offering:
  // "Tel Aviv", "Tel Aviv-Yafo, Tel Aviv, ISR" and "tel-aviv" were three
  // choices for one place, and none of them named a country.
  getStaticFacets("verified")
    .then((facets) => alertMsLocation.setOptions(normalizeLocationFacets(facets.locations)))
    .catch(() => {});
  loadGlobalCompanyOptions().then((opts) => {
    if (opts) alertMsCompany.setOptions(opts);
  });
}

function wireAlertCreateForm() {
  // Sign-out tears the panel down and sign-in builds a fresh one, but
  // editingAlertId lives above both. Left set, the rebuilt form would
  // read "New Alert" while still holding a pointer to someone's
  // existing alert, and Create would quietly overwrite it.
  editingAlertId = null;

  document.getElementById("alert-f-search").addEventListener("input", (e) => {
    alertFormState.search = e.target.value.trim();
  });

  alertMsDepartment = createMultiSelect("alert-ms-department", {
    placeholder: "Categories",
    onChange: (values) => { alertFormState.department = values; },
  });
  alertMsSeniority = createMultiSelect("alert-ms-seniority", {
    placeholder: "Levels",
    options: Object.entries(SENIORITY_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => { alertFormState.seniority = values; },
  });
  alertMsCompany = createMultiSelect("alert-ms-company", {
    placeholder: "Companies",
    searchable: true,
    onChange: (values) => { alertFormState.company = values; },
  });
  alertMsLocation = createLocationSelect("alert-ms-location", {
    placeholder: "Locations",
    searchable: true,
    onChange: ({ countries, cities }) => {
      alertFormState.country = countries;
      alertFormState.city = cities;
    },
  });
  alertMsWorkplace = createMultiSelect("alert-ms-workplace", {
    placeholder: "Workplace",
    options: Object.entries(WORKPLACE_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => { alertFormState.workplace = values; },
  });
  populateAlertFilterOptions();

  document.getElementById("cancel-edit-btn").addEventListener("click", cancelEditAlert);
  paintAlertFormMode();

  document.getElementById("create-alert-btn").addEventListener("click", async (e) => {
    // Localized to the button, not a page-level spinner: the user
    // clicked one control and that control is what should look busy.
    // .btn-busy hides the label with color:transparent rather than
    // replacing it, so the button keeps its exact width mid-request.
    const btn = e.currentTarget;
    btn.classList.add("btn-busy");
    const raw = {
      search: alertFormState.search,
      department: alertFormState.department.join(","),
      seniority: alertFormState.seniority.join(","),
      company: alertFormState.company.join(","),
      country: alertFormState.country.join(","),
      city: alertFormState.city.join(","),
      workplace: alertFormState.workplace.join(","),
    };
    const filter = Object.fromEntries(Object.entries(raw).filter(([, v]) => v !== "" && v != null));
    // Read once: the request is awaited below, and a click on another row
    // in the meantime would otherwise redirect the save.
    const editing = editingAlertId;
    try {
      if (editing) {
        await authedFetch(`/me/alerts/${editing}`, { method: "PATCH", body: JSON.stringify({ filter }) });
      } else {
        await authedFetch("/me/alerts", { method: "POST", body: JSON.stringify({ filter }) });
      }
      showCreateAlertFeedback(editing ? "Alert saved." : "Alert created.");
      resetAlertForm();
      renderAlertsList(await loadMyAlerts());
    } catch (err) {
      const fallback = editing ? "Could not save alert." : "Could not create alert.";
      showCreateAlertFeedback(err.message || fallback, true);
    } finally {
      // The panel is torn down and rebuilt on sign-out, so this button
      // can be gone by the time the request settles.
      btn.classList.remove("btn-busy");
    }
  });
}

// The scrape-fast Lambda (EventBridge, not scrape-fast.yml -- see its own
// header comment) re-polls every 5 min; 2 min keeps an open tab
// reasonably current without hammering the API, and lines up with
// CloudFront's own 120s cache on /api/* so most polls never even reach
// the Lambda.
const STATS_POLL_MS = 120_000;

async function boot() {
  // Pages other than the board load this file for its auth helpers and
  // createMultiSelect, and have none of the board's markup. Everything
  // below assumes #jobs-body and the filter controls exist, so stop here
  // rather than throwing through a dozen null lookups. handleAuthRedirect
  // still runs: a sign-in can land on any page.
  await handleAuthRedirect();
  if (!document.getElementById("jobs-body")) {
    wireAuth();
    return;
  } // before wireAuth: a fresh token from a redirect must be in localStorage before the initial render; also before applyStateFromUrl below, since a code-exchange redirect strips the URL down to location.pathname first

  // localStorage first, as the new baseline, THEN the URL on top -- an
  // explicit query param always overrides a saved filter, never the
  // other way around (see applyStoredFilters' own comment). Both need to
  // run before wireFilters() creates the actual controls -- state.israel_only
  // (read at creation time by ms-location's pinned "Israel (only)"
  // checkbox) needs to already be right by then. applyStateToFilterUI()
  // below handles the rest (the multi-selects/#f-search/the view switch),
  // which all need wireFilters() to have already assigned msDepartment
  // etc. first.
  // The address as the visitor typed it, read once before anything can
  // rewrite it. loadJobs() below runs syncUrl(), which rebuilds the URL
  // from state, and ?job= and ?view= are not state until they have been
  // acted on. Reading location.search after that found them already gone,
  // so a deep link to a job opened nothing.
  const bootParams = new URLSearchParams(location.search);
  applyStoredFilters();
  applyStateFromUrl(location.search);

  wireAuth();
  // Not awaited. The board renders from localStorage the moment it can,
  // signed in or out, and the merged-in stars from other devices flip
  // themselves on whenever /me/saved gets back.
  syncSavedFromServer();
  wireFilters();
  applyStateToFilterUI();
  wireJobDetail();
  wireThemeToggle();
  loadTicker();
  await refreshStats();
  loadJobs();

  // ?view=matches opens Best matches directly, for the 404 page and any
  // other link that wants it. Not filter state: setView works out the
  // skills, and the next syncUrl drops the param.
  if (bootParams.get("view") === "matches") setView("matches");

  // A deep link to one specific job (see jobPermalink) opens after the
  // above, not folded into applyStateToFilterUI -- it's not a filter
  // control, and openJobDetail needs the DOM/state from everything above
  // to already be in place. loadJobs() just ran its own syncUrl() with
  // no job selected yet, so this needs its own explicit re-sync after.
  const deepLinkJobId = bootParams.get("job");
  if (deepLinkJobId) {
    await openJobDetail(deepLinkJobId);
    syncUrl();
  }

  // Back/Forward: the browser already changed the URL, so this only
  // ever reads it back into state/UI -- never pushes or replaces itself
  // (that would corrupt the very history entry the user just navigated
  // to). loadJobs() at the end still does its own replaceState, but
  // that's just re-serializing the same state to the same canonical
  // form, a no-op in practice.
  window.addEventListener("popstate", () => {
    applyStateFromUrl(location.search);
    applyStateToFilterUI();
    const jobId = new URLSearchParams(location.search).get("job");
    if (jobId) {
      if (jobId !== selectedJobId) openJobDetail(jobId);
    } else if (selectedJobId) {
      closeJobDetail();
    }
    loadJobs();
    loadTicker();
  });

  // Every poller below goes through this. A tab nobody is looking at
  // still ran all three of them, forever, and a backgrounded tab left
  // open overnight was quietly the largest single source of API Lambda
  // invocations on the whole system. document.hidden costs one property
  // read and gives back everything that tab was spending.
  //
  // The catch-up on becoming visible is the part that keeps this from
  // being a downgrade: you come back to the tab and it refreshes, rather
  // than showing you whatever was on screen when you left.
  function whenVisible(fn) {
    return () => {
      if (!document.hidden) fn();
    };
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    refreshStats();
    refreshFreshness();
    refreshPipelineStatus();
  });

  setInterval(whenVisible(() => {
    refreshStats();
    loadTicker();
    // Piggybacks on the same 2-min tick as the stats/ticker refresh
    // above, not a separate timer -- loadJobs() already has its own
    // "did the response actually change" guard (see its own comment),
    // so an open tab quietly picks up new listings without a page
    // reload, a scroll jump, or losing the open detail drawer, but
    // never re-renders (and never flickers) when nothing really did
    // change, which is the common case within one 2-min window.
    loadJobs();
  }), STATS_POLL_MS);
  // The countdown tick stays unconditional: it reads no network, it only
  // recomputes a number already in memory.
  setInterval(tickApiStatus, API_STATUS_TICK_MS);
  setInterval(whenVisible(refreshFreshness), HEALTH_POLL_MS);
  refreshPipelineStatus();
  setInterval(whenVisible(refreshPipelineStatus), HEALTH_POLL_MS);
}

boot();
