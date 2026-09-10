// The advanced statistics page.
//
// Deliberately its own script rather than a mode of app.js. That file is
// three thousand lines of filter, URL, auth and alert machinery, none of
// which this page uses. Everything here reads one JSON file and draws
// it, so the few helpers it needs are duplicated rather than imported,
// which keeps this page at zero coupling to the board.
//
// Data comes from /stats.json, which the applier publishes to the
// frontend bucket every fifteen minutes. CloudFront serves it with no
// Lambda in the path. /api/stats stays as the fallback for the window
// between a deploy and the next publish, and answers identically.

"use strict";

const SENIORITY_ORDER = [
  "intern", "junior", "mid", "senior", "staff", "principal",
  "lead", "manager", "director", "exec", "unstated",
];
const SENIORITY_LABELS = {
  intern: "Intern", junior: "Junior", mid: "Mid", senior: "Senior",
  staff: "Staff", principal: "Principal", lead: "Lead", manager: "Manager",
  director: "Director", exec: "Exec", unstated: "Unstated",
};
const WORKPLACE_LABELS = {
  remote: "Remote", hybrid: "Hybrid", onsite: "On site", unstated: "Not stated",
};
const ATS_LABELS = {
  greenhouse: "Greenhouse", ashby: "Ashby", smartrecruiters: "SmartRecruiters",
  workable: "Workable", lever: "Lever", comeet: "Comeet", workday: "Workday",
  recruitee: "Recruitee", personio: "Personio", teamtailor: "Teamtailor",
  jazzhr: "JazzHR", jsonld: "Career page",
};

function fmtInt(n) {
  return (n ?? 0).toLocaleString("en-US");
}

function fmtPct(part, whole) {
  if (!whole) return "0%";
  return `${Math.round((part / whole) * 100)}%`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function loadStats() {
  try {
    const r = await fetch("/stats.json", { cache: "no-store" });
    if (r.ok) return await r.json();
  } catch {
    // The static copy is missing for the window between a deploy and
    // the next publish. The API answers the same question.
  }
  const r = await fetch("/api/stats");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// Bars, in the same shape the homepage draws them: name, track, count.
// The first row is the green one, which is the One Voice Rule applied
// to a list: exactly one thing is pointed at.
function barList(rows, label, count) {
  const max = Math.max(1, ...rows.map((r) => r.n));
  return rows.map((r) => `
    <div class="bar-row">
      <div class="name">${escapeHtml(label(r))}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${(r.n / max) * 100}%"></div></div>
      <div class="n">${count ? count(r) : fmtInt(r.n)}</div>
    </div>`).join("");
}

// Category by seniority, as one grid rather than two lists. Two bar
// lists can say "there are many senior roles" and "there are many
// backend roles" and still not tell you whether there are many senior
// backend roles. A cell can.
//
// Five shade steps, not a continuous ramp. The system is flat and
// two-tone, and a gradient would be a third voice; five steps of the
// ink colour read as a heatmap without becoming one.
function heatmap(cells) {
  const byCategory = new Map();
  for (const c of cells) {
    if (!byCategory.has(c.category)) byCategory.set(c.category, { total: 0, by: {} });
    const row = byCategory.get(c.category);
    row.total += c.n;
    row.by[c.seniority] = (row.by[c.seniority] || 0) + c.n;
  }
  const categories = [...byCategory.entries()]
    .sort((a, b) => b[1].total - a[1].total)
    .slice(0, 10);
  const present = SENIORITY_ORDER.filter((s) => categories.some(([, row]) => row.by[s]));
  const max = Math.max(1, ...categories.flatMap(([, row]) => Object.values(row.by)));
  const shade = (n) => (n === 0 ? 0 : Math.min(5, Math.ceil((n / max) * 5)));

  const head = `<div class="hm-corner"></div>${present.map((s) =>
    `<div class="hm-col">${escapeHtml(SENIORITY_LABELS[s] || s)}</div>`).join("")}`;
  const body = categories.map(([cat, row]) => `
    <div class="hm-row">${escapeHtml(cat)}<span class="hm-total">${fmtInt(row.total)}</span></div>
    ${present.map((s) => {
      const n = row.by[s] || 0;
      return `<div class="hm-cell s${shade(n)}" title="${escapeHtml(cat)}, ${escapeHtml(SENIORITY_LABELS[s] || s)}: ${fmtInt(n)}">${n ? fmtInt(n) : ""}</div>`;
    }).join("")}`).join("");

  return `<div class="heatmap" style="--hm-cols:${present.length}">${head}${body}</div>`;
}

function renderMetrics(s) {
  const el = document.getElementById("stats-metrics");
  const cards = [
    { label: "Open Jobs", value: fmtInt(s.totals.open_jobs), sub: `${fmtInt(s.totals.companies_hiring)} companies hiring`, hl: true },
    { label: "New in 7 Days", value: `+${fmtInt(s.throughput.new_jobs_7d)}`, sub: `${fmtInt(s.throughput.new_jobs_24h)} in the last day` },
    { label: "Closed in 7 Days", value: `-${fmtInt(s.throughput.closed_jobs_7d)}`, sub: `${fmtInt(s.throughput.closed_jobs_24h)} in the last day` },
    { label: "Median Open Age", value: s.age.median_open_days == null ? "n/a" : `${Math.round(s.age.median_open_days)}d`, sub: `oldest ${s.age.oldest_open_days == null ? "n/a" : Math.round(s.age.oldest_open_days) + "d"}` },
  ];
  el.innerHTML = cards.map((c) => `
    <div class="metric-card ${c.hl ? "highlight" : ""}">
      <div class="label">${c.label}</div>
      <div>
        <div class="value">${c.value}</div>
        <div class="sub">${c.sub}</div>
      </div>
    </div>`).join("");
}

function renderPanels(s) {
  const el = document.getElementById("stats-panels");
  const open = s.totals.open_jobs;
  const skilled = s.skills_coverage ? s.skills_coverage.with_skills : 0;
  const workplace = (s.workplace || []).map((r) => ({ ...r, key: r.workplace }));
  const stated = workplace.filter((r) => r.key !== "unstated").reduce((a, r) => a + r.n, 0);

  el.innerHTML = `
    <div class="panel panel-wide">
      <div class="panel-title">Where the Jobs Are</div>
      <div class="panel-sub">Open listings by category and seniority. Most listings state no level, and that column is shown rather than dropped.</div>
      ${s.category_seniority && s.category_seniority.length
        ? heatmap(s.category_seniority)
        : '<div class="sub" style="color:var(--grey)">No category data yet.</div>'}
    </div>

    <div class="panel">
      <div class="panel-title">What They Ask For</div>
      <div class="panel-sub">Technologies named in the listing, across the ${fmtPct(skilled, open)} of open roles that name any.</div>
      ${s.top_skills && s.top_skills.length
        ? barList(s.top_skills.slice(0, 15), (r) => r.skill, (r) => fmtPct(r.n, skilled))
        : '<div class="sub" style="color:var(--grey)">No skills data yet.</div>'}
    </div>

    <div class="panel">
      <div class="panel-title">How They Work</div>
      <div class="panel-sub">${fmtPct(open - stated, open)} of employers do not say. That is the finding, so it gets its own bar.</div>
      ${barList(workplace, (r) => WORKPLACE_LABELS[r.key] || r.key, (r) => fmtPct(r.n, open))}
    </div>

    <div class="panel">
      <div class="panel-title">Who Runs the Boards</div>
      <div class="panel-sub">Which applicant-tracking system each open listing came from. Plumbing on the homepage, the subject here.</div>
      ${barList((s.open_jobs_by_ats || []).slice(0, 10), (r) => ATS_LABELS[r.ats] || r.ats, (r) => fmtPct(r.n, open))}
    </div>

    <div class="panel">
      <div class="panel-title">Fastest Growing, 7 Days</div>
      <div class="panel-sub">Companies adding the most new listings this week, regardless of how many they already had.</div>
      ${s.top_movers_7d && s.top_movers_7d.length
        ? barList(s.top_movers_7d, (r) => r.domain)
        : '<div class="sub" style="color:var(--grey)">Nothing new in the last 7 days.</div>'}
    </div>

    <div class="panel">
      <div class="panel-title">Seniority Spread</div>
      <div class="panel-sub">Of listings that state a level. Most do not.</div>
      ${barList((s.seniority_breakdown || []).map((r) => ({ ...r, key: r.seniority })),
        (r) => SENIORITY_LABELS[r.key] || r.key)}
    </div>`;
}

function renderNote(s) {
  const el = document.getElementById("stats-note");
  const ghost = s.ghost || {};
  const dormant = ghost.dormant_pct == null ? "" :
    ` ${Math.round(ghost.dormant_pct * 100)}% of open listings have been open longer than ${ghost.threshold_days} days.`;
  el.textContent = `Figures cover every verified listing tracked since 1 September 2026 and refresh every fifteen minutes.${dormant} Historical trends arrive as the archive fills.`;
}

function wireThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  const sync = () => {
    btn.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "Light" : "Dark";
  };
  sync();
  btn.addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    if (next === "dark") root.setAttribute("data-theme", "dark");
    else root.removeAttribute("data-theme");
    localStorage.setItem("iljobs_theme", next);
    sync();
  });
}

async function boot() {
  wireThemeToggle();
  try {
    const s = await loadStats();
    renderMetrics(s);
    renderPanels(s);
    renderNote(s);
    const intro = document.getElementById("stats-intro");
    if (intro && s.freshness && s.freshness.minutes_since_update != null) {
      const m = Math.round(s.freshness.minutes_since_update);
      intro.textContent = `Every open listing we track, as of ${m <= 1 ? "a minute" : m + " minutes"} ago.`;
    }
  } catch (err) {
    const msg = `<div class="error-state" style="grid-column:1/-1">Could not load statistics: ${escapeHtml(err.message)}</div>`;
    document.getElementById("stats-metrics").innerHTML = msg;
    document.getElementById("stats-panels").innerHTML = msg;
  }
}

boot();
