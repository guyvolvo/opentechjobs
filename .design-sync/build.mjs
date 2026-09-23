// Builds ds-bundle/ for the Claude Design project pinned in config.json.
// The frontend has no component package, so this ships the real
// stylesheet and fonts plus hand-authored preview cards whose markup is
// copied from what app.js and board.html render. When that markup
// changes, change the matching card here.
//
// Run from the repo root: node .design-sync/build.mjs

import { mkdirSync, rmSync, copyFileSync, writeFileSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const OUT = "ds-bundle";
const FRONT = "frontend";

rmSync(OUT, { recursive: true, force: true });
mkdirSync(join(OUT, "fonts"), { recursive: true });

// Designs only receive styles.css and what it imports, so the real
// stylesheet goes in under that name. Its @font-face urls are already
// relative to fonts/.
copyFileSync(join(FRONT, "style.css"), join(OUT, "styles.css"));
for (const f of readdirSync(join(FRONT, "fonts"))) copyFileSync(join(FRONT, "fonts", f), join(OUT, "fonts", f));

// SVGs as app.js writes them.
const STAR = '<svg class="star-mark" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.6l1.95 3.95 4.35.63-3.15 3.07.74 4.33L8 11.53l-3.89 2.05.74-4.33L1.7 6.18l4.35-.63z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
const ARROW = '<svg class="external-arrow" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true"><path d="M2.5 7.5 7.5 2.5M3.5 2.5h4v4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CHECK = '<svg class="rail-tick" viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 6.3 4.9 8.7 9.5 3.4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CLOSE = '<svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true"><path d="M3 3l8 8M11 3l-8 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
const CHIP_X = readFileSync(join(FRONT, "app.js"), "utf8").match(/'<svg class="chip-x"[\s\S]*?<\/svg>'/)[0]
  .replace(/'\s*\+\s*'/g, "").slice(1, -1);

// A monogram in place of a fetched logo, the same fallback app.js ends on.
const logo = (letter) => "data:image/svg+xml," + encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 44 44"><rect width="44" height="44" fill="#609966"/>`
  + `<text x="22" y="29" font-family="Helvetica" font-size="20" fill="#f2f0ef" text-anchor="middle">${letter}</text></svg>`);

const EST = '<span class="salary-est-label">Est.</span> ';
const skills = (list) => list.map((s) => `<button type="button" class="job-chip skill">${s}</button>`).join("");

function row({ title, seniority, company, meta, est = true, salary, skill, age, fresh = false, selected = false, starred = false }) {
  return `
<tr data-id="${title}" class="${selected ? "selected" : ""}" tabindex="-1">
  <td class="star-cell"><button class="star-btn ${starred ? "on" : ""}" aria-pressed="${starred}" aria-label="Save">${STAR}</button></td>
  <td class="logo-cell"><img class="company-logo listing" src="${logo(company[0])}" alt=""></td>
  <td class="main-cell">
    <div class="job-card-title"><span class="job-title-text">${title}</span> <span class="badge seniority">${seniority}</span></div>
    <div class="job-meta"><span class="job-company">${company}</span> · ${meta}<span class="meta-age"> · <span class="meta-age-value ${fresh ? "fresh" : ""}">${age}</span></span></div>
    <div class="job-chips"><span class="job-chip salary" data-salary-source="${est ? "table" : "disclosed"}">${est ? EST : ""}<span class="salary-range">${salary}</span></span>${skills(skill)}</div>
  </td>
  <td data-label="Age" class="age-cell ${fresh ? "fresh" : ""}">${age}</td>
</tr>`;
}

function option(label, n, on = false) {
  return `<label class="rail-option${on ? " on" : ""}"><input type="checkbox" ${on ? "checked" : ""}/>`
    + `<span class="rail-box" aria-hidden="true">${CHECK}</span>`
    + `<span class="rail-option-label">${label}</span><span class="rail-count">${n}</span></label>`;
}

const swatch = (t) => `<div class="ds-sw"><span style="background:var(${t})"></span><code>${t}</code></div>`;
const caption = (s) => `<div class="ds-cap">${s}</div>`;

const CARDS = [
  {
    group: "Foundations", name: "Colors", title: "Colors",
    body: `<div class="ds-pad ds-grid">${["--white", "--black", "--green", "--green-text", "--green-bright", "--grey-line",
      "--hover-bg", "--row-hover", "--row-selected", "--red", "--logo-band", "--load-bar"].map(swatch).join("")}</div>`
      + `<div class="ds-pad">${caption('Light theme. Set data-theme="dark" on &lt;html&gt; for the dark palette.')}</div>`,
  },
  {
    group: "Foundations", name: "Type", title: "Type",
    body: `<div class="ds-pad ds-stack">
<div style="font-family:var(--font-display);font-weight:800;font-size:32px;letter-spacing:-0.01em">Job Board</div>${caption("--font-display, Overused Grotesk. Topbar and section titles only.")}
<div style="font-family:var(--font-ui);font-size:16px;font-weight:600">Senior Backend Engineer</div>${caption("--font-ui, Source Sans 3")}
<div style="font-family:var(--font);font-size:13px">Wix · R&amp;D · Tel Aviv (Hybrid) · 2d</div>${caption("--font, Helvetica. Everything dense.")}
</div>`,
  },
  {
    group: "Controls", name: "Button", title: "Buttons",
    body: `<div class="ds-pad ds-stack">
<div class="ds-row"><button class="btn">Create Alert</button><button class="btn ghost">Reset</button><button class="btn ghost btn-small">Clear the search</button></div>
<div class="ds-row"><button class="btn ghost btn-small btn-danger">Delete account</button><button class="btn btn-small btn-danger-solid">Delete for good</button></div>
${caption(".btn · .btn.ghost · .btn-small · .btn.ghost.btn-danger · .btn.btn-danger-solid")}
</div>`,
  },
  {
    group: "Controls", name: "SegmentedControl", title: "Segmented control",
    body: `<div class="ds-pad ds-stack"><div class="ds-row"><div class="view-switch seg" role="tablist">
<button type="button" class="seg-btn active" role="tab" aria-selected="true">Tech roles</button><button type="button" class="seg-btn" role="tab">All roles</button><button type="button" class="seg-btn" role="tab">Best matches</button><button type="button" class="seg-btn" role="tab">Saved</button>
</div></div>${caption(".seg &gt; .seg-btn, .active on the current one")}</div>`,
  },
  {
    group: "Controls", name: "Chips", title: "Chips and badges",
    body: `<div class="ds-pad ds-stack">
<div class="active-chips">${["Tel Aviv", "Senior", "Hybrid"].map((c) => `<button type="button" class="chip"><span class="chip-label">${c}</span>${CHIP_X}</button>`).join("")}</div>
<div class="ds-row"><span class="badge seniority">Senior</span><span class="badge best-effort">best_effort</span><span class="badge closed">Closed</span></div>
<div class="ds-row"><span class="job-chip salary" data-salary-source="table">${EST}<span class="salary-range">₪32k–40k</span></span><span class="job-chip salary" data-salary-source="disclosed"><span class="salary-range">₪30,000/mo</span></span>${skills(["Python", "Kafka"])}</div>
${caption(".chip (active filter) · .badge.seniority / .best-effort / .closed · .job-chip.salary · .job-chip.skill")}
</div>`,
  },
  {
    group: "Filters", name: "FilterRailGroup", title: "Filter rail group", width: 320,
    body: `<div class="ds-pad"><aside class="filter-rail ds-static"><div class="rail-groups">
<section class="rail-group"><h3 class="rail-title">Location<span class="rail-summary">Tel Aviv</span></h3><div class="rail-body">
${option("Tel Aviv", "4,812", true)}${option("Herzliya", "1,203")}${option("Haifa", "688")}${option("Jerusalem", "541")}
<button type="button" class="rail-more">Show all (38)</button></div></section>
<section class="rail-group"><h3 class="rail-title">Seniority</h3><div class="rail-body">
${option("Junior", "912")}${option("Senior", "3,377", true)}${option("Lead", "604")}
</div></section></div></aside></div>`,
  },
  {
    group: "Listing", name: "JobRow", title: "Job rows",
    body: `<div class="ds-pad"><div class="board-list"><table class="jobs"><thead><tr><th class="th-listing">Listing</th><th class="th-age">Age</th></tr></thead><tbody>
${row({ title: "Senior Backend Engineer", seniority: "Senior", company: "Wix", meta: "R&amp;D · Tel Aviv (Hybrid)", salary: "₪32k–40k", skill: ["Python", "Kafka", "PostgreSQL"], age: "2d", fresh: true })}
${row({ title: "Data Engineer", seniority: "Mid", company: "Monday.com", meta: "Data Platform · Tel Aviv (Hybrid)", est: false, salary: "₪28k–35k", skill: ["Spark", "Airflow", "AWS"], age: "5d", selected: true, starred: true })}
${row({ title: "Frontend Team Lead", seniority: "Lead", company: "Riskified", meta: "Engineering · Tel Aviv (On-site)", salary: "₪38k–46k", skill: ["React", "TypeScript"], age: "3w" })}
</tbody></table></div></div>`,
  },
  {
    group: "Listing", name: "JobDetail", title: "Job detail pane", width: 560,
    body: `<div class="ds-pad"><div class="job-detail ds-static">
<button type="button" class="job-detail-close" aria-label="Close job detail">${CLOSE}</button>
<div class="job-detail-company"><img class="company-logo detail" src="${logo("W")}" alt=""><div class="job-detail-company-text"><span class="job-detail-company-name">Wix</span><span class="job-detail-company-place">Tel Aviv</span></div></div>
<h2 class="job-detail-title">Senior Backend Engineer <span class="badge seniority">Senior</span></h2>
<div class="job-detail-actions"><a class="job-detail-apply" href="#">Apply ${ARROW}</a><button type="button" class="job-detail-star">${STAR}<span>Save</span></button><button type="button" class="link job-detail-permalink">Save link</button></div>
<div class="job-detail-facts">
<div class="fact"><span class="fact-label">Salary</span><span class="fact-value"><span>${EST}<span class="salary-range">₪32k–40k</span></span></span></div>
<div class="fact"><span class="fact-label">Posted</span><span class="fact-value">2d ago</span></div>
<div class="fact"><span class="fact-label">Department</span><span class="fact-value">R&amp;D</span></div>
<div class="fact"><span class="fact-label">Workplace</span><span class="fact-value"><span class="fact-absent">-</span></span></div>
</div>
<div class="job-detail-section-title">About the role</div>
<div class="job-detail-description"><p>You'd own the services behind site publishing, in Python and Kafka with PostgreSQL underneath.</p></div>
</div></div>`,
  },
];

// Preview-only layout. Nothing here restyles a component.
const HARNESS = `.ds-pad{padding:24px}.ds-stack{display:flex;flex-direction:column;gap:14px}
.ds-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.ds-cap{font:12px var(--font);color:var(--grey);opacity:.75}
.ds-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}
.ds-sw{display:flex;gap:10px;align-items:center}.ds-sw span{width:40px;height:40px;border:1px solid var(--grey-line)}
.ds-sw code{font:12px var(--font);color:var(--black)}
.ds-static{position:relative!important;inset:auto!important;transform:none!important;visibility:visible!important;width:auto!important;max-height:none!important;height:auto!important;z-index:auto!important}`;

for (const c of CARDS) {
  const dir = join(OUT, "components", c.group.toLowerCase(), c.name);
  const body = c.width ? `<div style="max-width:${c.width}px">${c.body}</div>` : c.body;
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `${c.name}.html`), `<!-- @dsCard group="${c.group}" -->
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${c.title}</title>
<link rel="stylesheet" href="../../../styles.css">
<style>${HARNESS}</style>
</head><body>
${body}
</body></html>
`);
}

// README: the authored conventions, then an index of the cards.
const conventions = readFileSync(join(".design-sync", "conventions.md"), "utf8").trim();
const index = CARDS.map((c) => `- ${c.title}: components/${c.group.toLowerCase()}/${c.name}/${c.name}.html`).join("\n");
writeFileSync(join(OUT, "README.md"), `${conventions}\n\n## Preview cards\n\nEach card is working markup against styles.css. Copy from them.\n\n${index}\n`);

console.log(`built ${CARDS.length} cards into ${OUT}/`);
