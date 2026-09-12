// OpenTechJobs frontend. No framework, no build step, served straight
// from S3/CloudFront, runs as-shipped. Talks to the API at /api/*,
// same-origin (CloudFront routes /api/* to the Lambda).
//
// Starring a listing is localStorage-only: the API has no write
// endpoints or accounts, so there's no server side to hang that state off.

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
  q: "",
  keywords: "", // ';'-separated, ALL must appear (AND, not OR)
  department: [], // labeled "Category" in the UI; backend field stays "department"
  seniority: [],
  company: [], // multi-select; also set via clicking a company in the market panels
  location: [], // curated top raw location strings, not a geocoded facet
  workplace: [], // remote|hybrid|onsite
  confidence: "all", // no confidence filter in the UI; shown inline via badge instead
  // Default global, not Israel-only (2026-09-08, flipped on request --
  // was true, silently narrowing every first-time visit's board to a
  // fraction of the real listing count). "Israel (only)" is now an
  // opt-in pick, same as any other location -- see the pinned option on
  // msLocation below, and buildShareParams/applyStateFromUrl for the
  // matching URL-param flip (israel_only=1 now means the filter is ON,
  // not off).
  israel_only: false,
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

// Two breakpoints, because the detail panel has three layouts and only
// two of them behave the same way.
//
// Above 1300px it is an in-flow sticky column beside the list. At or
// below that the list has no room left to share, so the panel leaves the
// flow and comes back as a sheet over the board: right-hand under
// 1300px, full-screen under 960px (see style.css). Everything the sheet
// needs from JS -- the .open class, the scroll lock on the page behind
// it, skipping the scroll bookkeeping the in-flow layout needs -- is the
// same for both, so that is one query.
//
// The second is only for the swipe-to-close gesture, which is written
// against the bottom sheet's translateY and would fight the side sheet's
// translateX. Both mirror a real media query in style.css. Reading the
// width from a literal in two places is how these drift apart.
const DETAIL_SHEET_QUERY = window.matchMedia("(max-width: 1300px)");
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

// fetch helpers

async function getJSON(path) {
  const res = await fetch(`${API_BASE}${path}`);
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
  // Top Hiring Companies panel calls this with size=16 itself (a real,
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

const STATUS_LEVELS = {
  operational: { symbol: "status-positive", label: "Operational", text: "LIVE" },
  degraded: { symbol: "status-warning", label: "Degraded", text: "DEGRADED" },
  outage: { symbol: "status-negative", label: "No recent updates", text: "OFFLINE" },
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
  const level = !fresh ? "outage" : age > DEGRADED_AFTER_MINUTES ? "degraded" : "operational";
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
  el.classList.remove("degraded", "outage");
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
  card.querySelector(".value").innerHTML = `${statusIconHtml(level)}${escapeHtml(value)}`;
  card.querySelector(".sub").textContent = sub;
  const syncEl = card.querySelector(".sync-countdown");
  if (syncEl) syncEl.textContent = pipelineActivityText() ?? "";
  paintStatusIcon(document.getElementById("status-dot"), level);
  document.getElementById("status-text").textContent = STATUS_LEVELS[level].text;
}

function renderMetrics(stats) {
  const el = document.getElementById("metrics-grid");
  setLastCheckedAt(stats.freshness.last_checked);
  const status = apiStatusFields();
  const fresh = status.fresh;

  const cards = [
    {
      label: "Global Open Jobs",
      value: fmtInt(stats.totals.open_jobs),
      sub: `${fmtInt(stats.meta.open_jobs_best_effort)} more unverified`,
      hl: true,
    },
    {
      label: "Companies Hiring",
      value: fmtInt(stats.totals.companies_hiring),
      sub: "with a fresh open role",
    },
    {
      label: "New Listings in 7d",
      value: `+${fmtInt(stats.throughput.new_jobs_24h)}`,
      sub: `${fmtInt(stats.throughput.new_jobs_7d)} in 7d`,
      hl: stats.throughput.new_jobs_24h > 0,
    },
    {
      label: "Closed / Filled",
      value: `-${fmtInt(stats.throughput.closed_jobs_24h)}`,
      sub: `${fmtInt(stats.throughput.closed_jobs_7d)} in 7d`,
    },
    {
      label: "Median Open Age",
      value: fmtAge(stats.age.median_open_days),
      sub: `oldest ${fmtAge(stats.age.oldest_open_days)}`,
    },
    {
      id: "metric-api-status",
      label: "Data Health",
      // Raw HTML here, unlike every other card's value: this one leads
      // with the state glyph. The text beside it is our own constant or
      // a formatted number, never anything a listing supplied.
      value: `${statusIconHtml(status.level)}${escapeHtml(status.value)}`,
      sub: status.sub,
      sub2: pipelineActivityText(),
      // The first paint has to land on the same class the tick would set
      // a second later, or the tile flashes green before correcting.
      cls: status.level === "operational" ? "highlight" : status.level,
    },
  ];

  el.innerHTML = cards
    .map(
      (c) => `
      <div class="metric-card ${c.cls || (c.hl ? "highlight" : "")}" ${c.id ? `id="${c.id}"` : ""}>
        <div class="label">${c.label}</div>
        <div>
          <div class="value">${c.value}</div>
          <div class="sub">${c.sub}</div>
          ${c.sub2 ? `<div class="sync-countdown">${c.sub2}</div>` : ""}
        </div>
      </div>`
    )
    .join("");

  paintStatusIcon(document.getElementById("status-dot"), apiStatusFields().level);
  document.getElementById("status-text").textContent =
    STATUS_LEVELS[apiStatusFields().level].text;
}

// Recomputes from lastCheckedAt every 1s -- the sync countdown needs a
// real per-second tick to read as "live"; the AGO text along for the
// ride is a no-op most seconds, negligible cost either way.
const API_STATUS_TICK_MS = 1_000;

// Market-insight panels: who's hiring, what for, where. No ATS-vendor
// breakdown here; that's plumbing, not a market signal (still available
// as open_jobs_by_ats for anyone polling the raw API).
function renderBarList(rows, nameKey, { clickable = false } = {}) {
  const max = Math.max(1, ...rows.map((r) => r.n));
  return rows
    .map((r) => {
      const name = escapeHtml(r[nameKey]);
      return `
      <div class="bar-row ${clickable ? "clickable" : ""}" ${clickable ? `data-company="${name}"` : ""}>
        <div class="name">${clickable ? companyLogoImg(r[nameKey], 16) : ""}${name}</div>
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
      <div class="panel-title">Top Hiring Companies</div>
      ${renderBarList(stats.top_companies, "domain", { clickable: true })}
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
      ${renderGhostStat(stats.ghost, stats.totals.open_jobs)}
    </div>`;

  el.querySelectorAll("[data-company]").forEach((row) => {
    row.addEventListener("click", () => {
      state.company = [row.dataset.company];
      state.starred_only = false;
      state.offset = 0;
      document.getElementById("f-starred").checked = false;
      msCompany.setSelected(state.company);
      loadJobs();
      loadTicker();
      window.scrollTo({ top: document.getElementById("board").offsetTop - 60, behavior: "smooth" });
    });
  });
}

// job board

let lastJobsResponse = null;

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
    q: state.q,
    keywords: state.keywords,
    department: state.department.join(","),
    seniority: state.seniority.join(","),
    company: state.company.join(","),
    location: state.location.join(","),
    workplace: state.workplace.join(","),
    confidence: state.confidence,
    israel_only: state.israel_only ? "1" : "",
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
  if (state.q) p.set("q", state.q);
  if (state.keywords) p.set("keywords", state.keywords);
  if (state.department.length) p.set("department", state.department.join(","));
  if (state.seniority.length) p.set("seniority", state.seniority.join(","));
  if (state.company.length) p.set("company", state.company.join(","));
  if (state.location.length) p.set("location", state.location.join(","));
  if (state.workplace.length) p.set("workplace", state.workplace.join(","));
  if (state.confidence !== "all") p.set("confidence", state.confidence);
  // Global is the default now, so the filter only needs to appear in the
  // URL when it's ON, as israel_only=1 -- not the old inverted scheme
  // (default Israel-only, israel_only=0 to opt out).
  if (state.israel_only) p.set("israel_only", "1");
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
const SORTABLE_KEYS = new Set(["age", "title"]);

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
  if (p.has("q")) state.q = p.get("q");
  if (p.has("keywords")) state.keywords = p.get("keywords");
  for (const key of ["department", "seniority", "company", "location", "workplace"]) {
    if (p.has(key)) state[key] = p.get(key).split(",").filter(Boolean);
  }
  if (p.has("confidence")) state.confidence = p.get("confidence");
  if (p.has("israel_only")) state.israel_only = p.get("israel_only") === "1";
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
  "q", "keywords", "department", "seniority", "company", "location",
  "workplace", "confidence", "israel_only", "max_age_days", "starred_only",
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
  document.getElementById("f-q").value = state.q;
  document.getElementById("f-keywords").value = state.keywords;
  document.getElementById("f-date-posted").value = state.max_age_days || "";
  document.getElementById("f-starred").checked = state.starred_only;
  msDepartment.setSelected(state.department);
  msSeniority.setSelected(state.seniority);
  msCompany.setSelected(state.company);
  msLocation.setSelected(state.location);
  msWorkplace.setSelected(state.workplace);
  // The pinned "Israel (only)" checkbox isn't one of msLocation's own
  // selected values (createMultiSelect only set its initial checked
  // state once, at wireFilters() time) -- without this a Back/Forward
  // navigation could leave it visually out of sync with state.israel_only.
  document.getElementById("f-israel").checked = state.israel_only;
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

async function loadJobs() {
  // Every state-mutating handler in this file calls loadJobs() right
  // after, so state is already final for this transition -- one call
  // here covers all of them instead of one at each call site.
  syncUrl();
  saveFiltersToStorage();
  updateFiltersToggleLabel();

  const tbody = document.getElementById("jobs-body");
  const starred = getStarred();
  renderCompanyChip();

  if (state.starred_only) {
    renderStarredOnly(starred);
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
    const data = await getJSON(`/jobs?${params}`);
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
    setLoadBar(false);
  }
}

// Every empty state says the same two things: that there is nothing to
// show, then why.
function emptyState(line) {
  return `<strong>No results</strong><span>${line}</span>`;
}

function renderStarredOnly(starred) {
  document.getElementById("jobs-loading").textContent = "";
  document.getElementById("jobs-error").style.display = "none";
  const rows = lastJobsResponse?.jobs?.filter((j) => starred.has(j.id)) || [];
  if (!rows.length) {
    document.getElementById("jobs-empty").innerHTML = emptyState("You have not starred any listings yet.");
    document.getElementById("jobs-empty").style.display = "block";
    document.getElementById("jobs-body").innerHTML = "";
    document.getElementById("result-count").innerHTML = "";
    return;
  }
  document.getElementById("jobs-empty").style.display = "none";
  renderJobRows(rows, starred);
  document.getElementById("result-count").innerHTML = `<b>${rows.length}</b> starred`;
  document.getElementById("pagination").style.display = "none";
}

function renderJobs(data, starred) {
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
    `<b>${from}–${to}</b> of <b>${fmtInt(data.total)}</b> open listings`;
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
  const parts = [escapeHtml(companyLabel(j))];
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
function jobSkillsHtml(j) {
  const skills = (j.skills || "").split(",").filter(Boolean);
  if (!skills.length) return "";
  const chips = skills
    .map((s) => `<button class="skill-chip" data-skill="${escapeHtml(s)}" type="button">${escapeHtml(s)}</button>`)
    .join("");
  return `<span class="skill-bracket">[</span>${chips}<span class="skill-bracket">]</span>`;
}

function renderJobRows(jobs, starred) {
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
          <button class="star-btn ${isStarred ? "on" : ""}" data-star="${j.id}" title="Star (saved in this browser only)">
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
            </div>
            <div class="job-meta">${jobMetaLine(j)}</div>
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
      if (state.starred_only) loadJobs();
    });
  });

  document.querySelectorAll("[data-copy-url]").forEach((btn) => {
    btn.addEventListener("click", () => copyToClipboard(btn, btn.dataset.copyUrl));
  });

  document.querySelectorAll("[data-skill]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // same reasoning as the star button above
      state.keywords = btn.dataset.skill;
      state.offset = 0;
      document.getElementById("f-keywords").value = state.keywords;
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

  if (DETAIL_SHEET_QUERY.matches) {
    // Sheet layout (see style.css): lock the page behind it so the
    // sheet's own scroll doesn't also scroll the list underneath, and
    // slide it in on the next frame. The class goes on after hidden=false
    // has painted, or there's no off-screen starting position for the
    // transition to animate from.
    document.body.style.overflow = "hidden";
    requestAnimationFrame(() => {
      panel.classList.add("open");
      document.getElementById("job-scrim")?.classList.add("open");
    });
  }
  // Above 1300px there is deliberately nothing to do. The panel is a
  // sticky in-flow column that's already in view, and the old code that
  // scrolled the page to find it was compensating for the stacked layout
  // that used to exist below 1300px. That layout is gone.

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
  // Where the row sat on screen before the panel goes away. Reported
  // live: closing a listing dumped the reader at the bottom of the page
  // instead of back where they were. In the wide layout the panel is
  // part of the document flow, so removing it can make the page shorter
  // and the browser clamps the scroll position to the new maximum, which
  // is the footer. The sheet layouts are position:fixed and never affect
  // the page's height, so this is a no-op there.
  //
  // Anchored to the row rather than to a saved scrollY, because the
  // document height changes underneath: restoring a raw offset would
  // land somewhere else, or be clamped away entirely. Keeping the row
  // visually still is what "where I was" actually means.
  const anchorRow = selectedJobId
    ? document.querySelector(`tr[data-id="${selectedJobId}"]`)
    : null;
  const anchorTop = anchorRow ? anchorRow.getBoundingClientRect().top : null;

  if (DETAIL_SHEET_QUERY.matches) {
    panel.classList.remove("open");
    document.getElementById("job-scrim")?.classList.remove("open");
    document.body.style.overflow = "";
    // Delayed to match style.css's 0.25s slide-out transition -- an
    // immediate hidden=true would cut straight to display:none, same as
    // no animation at all. Cleared by the next openJobDetail (see its
    // own comment) so switching jobs mid-close can't get yanked shut.
    jobDetailCloseTimer = setTimeout(() => {
      panel.hidden = true;
      panel.innerHTML = "";
    }, 250);
  } else {
    panel.hidden = true;
    panel.innerHTML = "";
  }
  document.querySelector(`tr[data-id="${selectedJobId}"]`)?.classList.remove("selected");
  selectedJobId = null;

  // Put the row back where it was. Skipped on either sheet layout, which
  // is position:fixed and never affected the page's height to begin
  // with, and skipped when the row isn't on this page at all (a deep
  // link, or the list moved on underneath). Instant, not smooth: this is undoing
  // an unwanted jump, and animating it would draw attention to the very
  // movement it exists to hide.
  if (!DETAIL_SHEET_QUERY.matches && anchorRow && anchorTop !== null) {
    const drift = anchorRow.getBoundingClientRect().top - anchorTop;
    if (Math.abs(drift) > 1) {
      window.scrollTo({ top: window.scrollY + drift, behavior: "auto" });
    }
  }
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
  // covering the page, and it's the nearest target at the width where
  // the scrim exists at all: the close button is over on the far side of
  // the sheet, but the thing the reader is looking at is the list.
  document.getElementById("job-scrim")?.addEventListener("click", () => {
    if (selectedJobId !== null) closeJobDetailAndSync();
  });

  // Crossing 1300px with a job open swaps the panel between an in-flow
  // column and a sheet, and the scroll lock belongs to only one of them.
  // Without this, resizing from a sheet to the wide layout leaves the
  // page permanently unscrollable with nothing on screen to explain it.
  DETAIL_SHEET_QUERY.addEventListener("change", (e) => {
    const scrim = document.getElementById("job-scrim");
    const panel = document.getElementById("job-detail");
    if (e.matches && selectedJobId !== null) {
      document.body.style.overflow = "hidden";
      panel.classList.add("open");
      scrim?.classList.add("open");
    } else {
      document.body.style.overflow = "";
      panel.classList.remove("open");
      scrim?.classList.remove("open");
    }
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

document.addEventListener("click", () => OPEN_MULTISELECTS.forEach((closeOther) => closeOther()));

// filter wiring

function wireFilters() {
  document.getElementById("f-q").addEventListener(
    "input",
    debounce((e) => {
      state.q = e.target.value.trim();
      state.offset = 0;
      loadJobs();
      loadTicker();
    }, 300)
  );

  document.getElementById("f-keywords").addEventListener(
    "input",
    debounce((e) => {
      state.keywords = e.target.value.trim();
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

  msLocation = createMultiSelect("ms-location", {
    placeholder: "Locations",
    searchable: true,
    pinnedOption: {
      id: "f-israel",
      label: "Israel (only)",
      checked: state.israel_only,
      onChange: (checked) => {
        state.israel_only = checked;
        state.offset = 0;
        loadJobs();
        loadTicker();
      },
    },
    onChange: (values) => {
      state.location = values;
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

  document.getElementById("f-starred").addEventListener("change", (e) => {
    state.starred_only = e.target.checked;
    loadJobs();
    // Not loadTicker(): "starred" is a client-local view, not an API filter.
  });

  document.getElementById("f-reset").addEventListener("click", () => {
    state.q = "";
    state.keywords = "";
    state.department = [];
    state.seniority = [];
    state.company = [];
    state.location = [];
    state.workplace = [];
    state.israel_only = false;
    state.max_age_days = "";
    state.starred_only = false;
    state.sort = "age";
    state.dir = "asc";
    state.offset = 0;
    document.getElementById("f-q").value = "";
    document.getElementById("f-keywords").value = "";
    document.getElementById("f-date-posted").value = "";
    msDepartment.reset();
    msSeniority.reset();
    msCompany.reset();
    msLocation.reset();
    msWorkplace.reset();
    document.getElementById("f-israel").checked = false;
    document.getElementById("f-starred").checked = false;
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
// Counts against #f-q deliberately excluded: it's always visible on
// its own, never one of the controls this button is hiding.
function updateFiltersToggleLabel() {
  let n = 0;
  if (state.keywords) n++;
  if (state.department.length) n++;
  if (state.seniority.length) n++;
  if (state.company.length) n++;
  if (state.location.length) n++;
  if (state.workplace.length) n++;
  if (state.israel_only) n++; // the location dropdown's own pinned "Israel (only)" checkbox
  if (state.max_age_days) n++;
  if (state.starred_only) n++;
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
async function refreshFacetOptions() {
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
    msDepartment.setOptions(facets.categories.map((r) => ({ value: r.value, label: `${r.value} (${r.n})` })));
    msLocation.setOptions(facets.locations.map((r) => ({ value: r.value, label: `${r.value} (${r.n})` })));
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
  // Cleared on a timer, so a second click mid-fade restarts the window
  // rather than letting the first one strip the class out from under it.
  let themeFadeTimer = null;
  btn.addEventListener("click", () => {
    const root = document.documentElement;
    // Only ever on during the swap itself. See .theme-transition in
    // style.css for why this isn't just left on permanently.
    root.classList.add("theme-transition");
    clearTimeout(themeFadeTimer);
    themeFadeTimer = setTimeout(() => root.classList.remove("theme-transition"), 450);

    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    if (next === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    localStorage.setItem(THEME_KEY, next);
    sync();
  });
}

// Topbar ticker: 10 most recent listings matching the board's current
// filters, not a fixed sitewide list. Called from every filter-changing
// handler, but not pagination/sort (those don't change what "recent"
// means). Duplicated once in the DOM so the CSS marquee loops seamlessly.
async function loadTicker() {
  setLoadBar(true);
  try {
    return await _loadTicker();
  } finally {
    setLoadBar(false);
  }
}

async function _loadTicker() {
  const track = document.getElementById("ticker-track");
  try {
    const params = qs({ ...currentFilterParams(), limit: 10, sort: "age", dir: "asc" });
    const data = await getJSON(`/jobs?${params}`);
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
    track.style.animationDuration = `${data.jobs.length * 4}s`;
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
  }

  setLoadBar(true);
  try {
    const stats = await getStaticOrApi("/stats.json", "/stats");
    latestStats = stats;
    renderMetrics(stats);
    renderPanels(stats);
    refreshFacetOptions();
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
    }
  } finally {
    setLoadBar(false);
  }
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
  area.innerHTML = `
    <button class="auth-trigger" id="topbar-alert-btn" type="button">+ Alert</button>
    <button class="auth-trigger" id="auth-trigger" type="button">${escapeHtml(email)}</button>
    <div class="auth-panel alerts-panel" id="auth-panel" hidden>
      <div class="alerts-header alerts-header-row">
        <span>My Alerts</span>
        <a class="link account-link" href="/account.html">Account</a>
      </div>
      <div id="alerts-list"><p class="alerts-empty">Loading…</p></div>

      <div class="alert-create" id="alert-create">
        <div class="alerts-header" id="alert-form-title">New Alert</div>
        <div class="alert-create-fields">
          <input type="text" id="alert-f-q" placeholder="SEARCH TITLE, COMPANY, LOCATION…" />
          <div class="ms" id="alert-ms-department"></div>
          <div class="ms" id="alert-ms-seniority"></div>
          <div class="ms" id="alert-ms-company"></div>
          <div class="ms" id="alert-ms-location"></div>
          <div class="ms" id="alert-ms-workplace"></div>
          <label class="toggle"><input type="checkbox" id="alert-f-israel" /> Israel only</label>
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
function describeAlertFilter(filter) {
  const parts = [];
  if (filter.q) parts.push(`"${filter.q}"`);
  if (filter.department) parts.push(filter.department.split(",").join(", "));
  if (filter.seniority) parts.push(filter.seniority.split(",").join(", "));
  if (filter.company) parts.push(filter.company.split(",").join(", "));
  if (filter.location) parts.push(filter.location.split(",").join(", "));
  if (filter.workplace) parts.push(filter.workplace.split(",").join(", "));
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
  q: "",
  department: [],
  seniority: [],
  company: [],
  location: [],
  workplace: [],
  // Default global, matching the board's own default (see state.israel_only
  // above) -- was true, silently scoping every new alert to Israel-only
  // unless a user noticed and unchecked it first.
  israel_only: false,
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
  alertFormState.q = filter.q || "";
  alertFormState.department = list(filter.department);
  alertFormState.seniority = list(filter.seniority);
  alertFormState.company = list(filter.company);
  alertFormState.location = list(filter.location);
  alertFormState.workplace = list(filter.workplace);
  alertFormState.israel_only = filter.israel_only === "1";

  document.getElementById("alert-f-q").value = alertFormState.q;
  document.getElementById("alert-f-israel").checked = alertFormState.israel_only;
  alertMsDepartment.setSelected(alertFormState.department);
  alertMsSeniority.setSelected(alertFormState.seniority);
  alertMsCompany.setSelected(alertFormState.company);
  alertMsLocation.setSelected(alertFormState.location);
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
  alertFormState.q = "";
  alertFormState.department = [];
  alertFormState.seniority = [];
  alertFormState.company = [];
  alertFormState.location = [];
  alertFormState.workplace = [];
  alertFormState.israel_only = false;
  document.getElementById("alert-f-q").value = "";
  document.getElementById("alert-f-israel").checked = false;
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
    alertMsLocation.setOptions(
      latestStats.top_locations.map((r) => ({ value: r.location, label: `${r.location} (${r.n})` }))
    );
  }
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

  document.getElementById("alert-f-q").addEventListener("input", (e) => {
    alertFormState.q = e.target.value.trim();
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
  alertMsLocation = createMultiSelect("alert-ms-location", {
    placeholder: "Locations",
    searchable: true,
    onChange: (values) => { alertFormState.location = values; },
  });
  alertMsWorkplace = createMultiSelect("alert-ms-workplace", {
    placeholder: "Workplace",
    options: Object.entries(WORKPLACE_LABELS).map(([value, label]) => ({ value, label })),
    onChange: (values) => { alertFormState.workplace = values; },
  });
  populateAlertFilterOptions();

  document.getElementById("alert-f-israel").addEventListener("change", (e) => {
    alertFormState.israel_only = e.target.checked;
  });

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
      q: alertFormState.q,
      department: alertFormState.department.join(","),
      seniority: alertFormState.seniority.join(","),
      company: alertFormState.company.join(","),
      location: alertFormState.location.join(","),
      workplace: alertFormState.workplace.join(","),
      israel_only: alertFormState.israel_only ? "1" : "",
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
  // below handles the rest (the multi-selects/#f-q/#f-keywords/#f-starred),
  // which all need wireFilters() to have already assigned msDepartment
  // etc. first.
  applyStoredFilters();
  applyStateFromUrl(location.search);

  wireAuth();
  wireFilters();
  applyStateToFilterUI();
  wireJobDetail();
  wireThemeToggle();
  loadTicker();
  await refreshStats();
  loadJobs();

  // A deep link to one specific job (see jobPermalink) opens after the
  // above, not folded into applyStateToFilterUI -- it's not a filter
  // control, and openJobDetail needs the DOM/state from everything above
  // to already be in place. loadJobs() just ran its own syncUrl() with
  // no job selected yet, so this needs its own explicit re-sync after.
  const deepLinkJobId = new URLSearchParams(location.search).get("job");
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
