// The Explore page: questions over every listing, run in the browser.
//
// Two ways to ask. The builder, which most visitors will stay in: pick
// which listings, how to group them, what to show, and any number of
// filters, and qb.js writes the SQL. Or the SQL itself, for anyone who
// wants what the builder cannot express. The builder's controls are the
// board's own filter-bar vocabulary, including a port of its hand-built
// multi-select, so this page has no native-looking controls on it.
//
// The database is /explore.db, a static file the applier rebuilds hourly
// (loader/build_explore.py). SQLite compiled to WebAssembly reads it
// through a virtual filesystem that fetches only the 4KB pages a query
// touches, by HTTP range request. No server runs anything, and a query
// can only exhaust this tab, which is the whole security model.

"use strict";

// Absolute from the site root, all three, and it matters for the last
// one: the worker resolves the wasm path relative to ITS location, not
// the page's, so a page-relative "vendor/httpvfs/sql-wasm.wasm" became
// /vendor/httpvfs/vendor/httpvfs/sql-wasm.wasm and the browser tried to
// instantiate an S3 404 document as WebAssembly. Reported live as
// "expected magic word 00 61 73 6d, found 3c 3f 78 6d", which is
// "<?xm". The library's own README uses absolute URLs for this reason.
const MANIFEST_URL = "/explore.json";   // names the current build; see loader/build_explore.py
const WORKER_URL = "/vendor/httpvfs/sqlite.worker.js";
const WASM_URL = "/vendor/httpvfs/sql-wasm.wasm";
// 64 KB per HTTP read, sixteen database pages at a time. The questions
// this page asks are index scans of a few megabytes, and at 4 KB that
// was thousands of round trips: measured against the live file, the
// opening question took 1,203 ms at 4 KB and 122 ms at 64 KB for the
// same bytes.
const CHUNK_SIZE = 65536;
const MAX_BYTES = 256 * 1024 * 1024;  // per page load, then the worker refuses
const MAX_ROWS = 2000;         // painted, not computed
const QUERY_TIMEOUT_MS = 60_000;

// Starting questions. Builder states where the builder can express them,
// plain SQL where it cannot. Clicking one loads it and runs it.
const TEMPLATES = [
  { name: "Open jobs by category",
    state: { status: "open", group: "category", metric: "count", limit: 25 } },
  { name: "Seniority spread",
    state: { status: "open", group: "seniority", metric: "share", limit: 25 } },
  { name: "Most-asked-for skills",
    state: { status: "open", group: "skill", metric: "count", limit: 25 } },
  { name: "Skills asked of senior engineers",
    state: { status: "open", group: "skill", metric: "count", limit: 20,
             filters: [{ field: "seniority", values: ["senior"] },
                       { field: "category", values: ["Software Engineering"] }] } },
  { name: "Remote, hybrid, on site, or unsaid",
    state: { status: "open", group: "workplace", metric: "share", limit: 10 } },
  { name: "Who discloses pay, by ATS",
    sql: `SELECT ats,
       COUNT(*) AS listings,
       SUM(salary_source = 'disclosed') AS disclosed,
       ROUND(100.0 * SUM(salary_source = 'disclosed') / COUNT(*), 1) AS pct_disclosed
FROM jobs
WHERE closed_at IS NULL
GROUP BY ats
HAVING listings > 300
ORDER BY pct_disclosed DESC` },
  { name: "New listings per day",
    state: { status: "all", group: "day", metric: "count", limit: 60 } },
  { name: "Days open before closing, by category",
    state: { status: "closed", group: "category", metric: "avg_days_open", limit: 25 } },
  { name: "Companies hiring the most",
    state: { status: "open", group: "company", metric: "count", limit: 30 } },
  { name: "Senior backend roles in Israel",
    state: { status: "open", group: "none", limit: 100,
             filters: [{ field: "seniority", values: ["senior"] },
                       { field: "category", values: ["Software Engineering"] },
                       { field: "location", text: "Israel" }] } },
  { name: "Listings that state a real salary",
    state: { status: "open", group: "none", limit: 100,
             filters: [{ field: "salary_source", values: ["disclosed"] }] } },
];

const SCHEMA = [
  { table: "jobs", note: "One row per listing, open and closed.",
    columns: [["id", "text", "stable id"], ["company", "text", "domain; joins to companies"], ["ats", "text", "which system it came from"],
      ["title", "text", ""], ["category", "text", "our normalisation; NULL when unsure"], ["department", "text", "the employer's own label"],
      ["seniority", "text", "intern to exec, or NULL"], ["workplace", "text", "remote, hybrid, onsite, or NULL"], ["location", "text", "as written"],
      ["salary_text", "text", "as shown on the board"], ["salary_source", "text", "disclosed, table, estimated, or NULL"], ["url", "text", ""],
      ["posted_at", "text", "employer's date, if given"], ["first_seen", "text", "when we first saw it"], ["last_seen", "text", ""],
      ["closed_at", "text", "NULL while open"], ["days_open", "real", "closed minus first seen, or age so far"]] },
  { table: "job_skills", note: "One row per skill per listing, with the listing's filter columns.",
    columns: [["job_rowid", "integer", "jobs.rowid"], ["job_id", "text", ""], ["skill", "text", "lower-cased"], ["company", "text", ""], ["ats", "text", ""], ["category", "text", ""],
      ["seniority", "text", ""], ["workplace", "text", ""], ["salary_source", "text", ""], ["first_seen", "text", ""], ["closed_at", "text", "NULL while open"], ["days_open", "real", ""]] },
  { table: "companies", note: "One row per tracked company.", columns: [["domain", "text", ""], ["name", "text", "as the ATS reports it"], ["ats", "text", ""], ["open_jobs", "integer", ""]] },
  { table: "facets", note: "Distinct values per filter, with counts.", columns: [["field", "text", ""], ["value", "text", ""], ["label", "text", "companies only"], ["n", "integer", "open listings"]] },
  { table: "meta", note: "When this file was built.", columns: [["key", "text", ""], ["value", "text", ""]] },
];

const LABELS = {
  seniority: { intern: "Intern", junior: "Junior", mid: "Mid", senior: "Senior", staff: "Staff", principal: "Principal", lead: "Lead", manager: "Manager", director: "Director", exec: "Executive" },
  workplace: { remote: "Remote", hybrid: "Hybrid", onsite: "On site" },
  salary_source: { disclosed: "Disclosed by employer", table: "Estimated (Israeli table)", estimated: "Estimated (learned)" },
  ats: { greenhouse: "Greenhouse", ashby: "Ashby", smartrecruiters: "SmartRecruiters", workable: "Workable", lever: "Lever", comeet: "Comeet", workday: "Workday", recruitee: "Recruitee", personio: "Personio", teamtailor: "Teamtailor", jazzhr: "JazzHR", jsonld: "Career page" },
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtInt = (n) => Number(n).toLocaleString("en-US");
const fmtBytes = (b) => (b < 1024 * 1024 ? `${Math.round(b / 1024)} KB` : `${(b / (1024 * 1024)).toFixed(1)} MB`);

let worker = null;
let running = false;
let mode = "builder";
let viz = "auto";
let state = QB.defaultState();
let pickerValues = {};   // field -> [{value,label}], from the database itself
let lastResult = null;   // {columns, values} of the last answer, for the CSV export
const filterWidgets = new Map();  // filter index -> multi-select handle

// A port of the board's multi-select (app.js createMultiSelect), trimmed
// to what this page uses. Same markup, same classes, so it is the same
// control. Duplicated rather than imported: app.js is three thousand
// lines of board machinery this page has no use for.
const OPEN_MENUS = new Set();
document.addEventListener("click", () => OPEN_MENUS.forEach((close) => close()));

function createMultiSelect(container, { placeholder, options = [], searchable = false, onChange, selected: initial = [] }) {
  const selected = new Set(initial);
  container.innerHTML = `
    <button type="button" class="ms-toggle" aria-haspopup="listbox" aria-expanded="false">${escapeHtml(placeholder)}</button>
    <div class="ms-menu" hidden>
      ${searchable ? '<input type="text" class="ms-search" placeholder="Filter…" />' : ""}
      <div class="ms-options" role="listbox"></div>
      <button type="button" class="ms-clear">Clear</button>
    </div>`;
  const toggle = container.querySelector(".ms-toggle");
  const menu = container.querySelector(".ms-menu");
  const optionsEl = container.querySelector(".ms-options");
  const searchEl = container.querySelector(".ms-search");

  function renderOptions(filterText = "") {
    const q = filterText.trim().toLowerCase();
    const visible = q ? options.filter((o) => o.label.toLowerCase().includes(q)) : options;
    optionsEl.innerHTML = visible.slice(0, 400).map((o) => `
      <label class="ms-option">
        <input type="checkbox" value="${escapeHtml(o.value)}" ${selected.has(o.value) ? "checked" : ""} />
        ${escapeHtml(o.label)}
      </label>`).join("") || `<div class="ms-empty">${options.length ? "No matches." : "Loading values…"}</div>`;
  }
  function updateLabel() {
    if (selected.size === 0) toggle.textContent = placeholder;
    else if (selected.size === 1) {
      const v = [...selected][0];
      const o = options.find((x) => x.value === v);
      toggle.textContent = o ? o.label : v;
    } else toggle.textContent = `${selected.size} selected`;
    toggle.classList.toggle("active", selected.size > 0);
  }
  function close() { menu.hidden = true; toggle.setAttribute("aria-expanded", "false"); OPEN_MENUS.delete(close); }
  function open() { OPEN_MENUS.forEach((c) => c()); menu.hidden = false; toggle.setAttribute("aria-expanded", "true"); OPEN_MENUS.add(close); if (searchEl) searchEl.focus(); }

  toggle.addEventListener("click", (e) => { e.stopPropagation(); menu.hidden ? open() : close(); });
  menu.addEventListener("click", (e) => e.stopPropagation());
  optionsEl.addEventListener("change", (e) => {
    if (!e.target.matches('input[type="checkbox"]')) return;
    e.target.checked ? selected.add(e.target.value) : selected.delete(e.target.value);
    updateLabel();
    onChange([...selected]);
  });
  if (searchEl) searchEl.addEventListener("input", () => renderOptions(searchEl.value));
  container.querySelector(".ms-clear").addEventListener("click", () => {
    selected.clear(); renderOptions(searchEl ? searchEl.value : ""); updateLabel(); onChange([]);
  });
  renderOptions();
  updateLabel();
  return { close };
}

// Database.
async function openDatabase() {
  const status = $("explore-status");
  status.textContent = "Opening the database…";
  // Each hourly build is its own file, so the pages this session caches
  // all come from one build, whatever gets published while it is open.
  // Until the first versioned build has been published the manifest is
  // missing, and the old fixed name still works.
  const res = await fetch(MANIFEST_URL, { cache: "no-store" });
  const url = res.ok ? (await res.json()).url : "/explore.db";
  const config = { from: "inline", config: { serverMode: "full", url, requestChunkSize: CHUNK_SIZE } };
  worker = await createDbWorker([config], WORKER_URL, WASM_URL, MAX_BYTES);  // eslint-disable-line no-undef

  const meta = Object.fromEntries((await worker.db.exec("SELECT key, value FROM meta"))[0]?.values || []);
  status.textContent = `${fmtInt(meta.jobs || 0)} listings, ${fmtInt(meta.companies || 0)} companies`;

}

// Throw the worker away and start another. The old one may still be
// grinding through a query nobody wants; terminating it is the only
// way to stop it, and the new one starts with an empty page cache.
async function reopenDatabase() {
  try { worker?.raw?.terminate(); } catch { /* already gone */ }
  worker = null;
  try { await openDatabase(); } catch (e) { $("explore-status").textContent = "The database could not be reopened. Reload the page."; }
}

// The pickers' options come from the data, so a new ATS or category
// shows up on its own. They are read from the facets table the builder
// precomputes: one small indexed query for everything, not one GROUP BY
// over 111,000 rows per field. Reported live: the page sat with blank
// controls until seven such scans had pulled most of the file through
// range requests on a cold cache, and toggling modes in the meantime
// read the empty controls back into state.
//
// This runs after the builder is on screen, and each multi-select picks
// its options up when it is next rendered. A file built before the
// facets table existed falls back to the scans, which is slow but
// correct, and only for the hour until the next build.
async function loadPickerValues() {
  const label = (field, v, name) => `${name || (LABELS[field] || {})[v] || v}`;
  try {
    const r = await worker.db.exec("SELECT field, value, label, n FROM facets ORDER BY field, n DESC");
    const by = {};
    for (const [field, v, name, n] of r[0]?.values || []) {
      (by[field] ||= []).push({ value: v, label: `${label(field, v, name)} (${fmtInt(n)})` });
    }
    if (Object.keys(by).length) { pickerValues = by; return; }
  } catch {
    // No facets table in this build of the file. Fall through.
  }
  const cols = ["category", "seniority", "workplace", "ats", "salary_source"];
  for (const col of cols) {
    const r = await worker.db.exec(`SELECT ${col}, COUNT(*) n FROM jobs WHERE closed_at IS NULL AND ${col} IS NOT NULL GROUP BY 1 ORDER BY n DESC`);
    pickerValues[col] = (r[0]?.values || []).map(([v, n]) => ({ value: v, label: `${label(col, v)} (${fmtInt(n)})` }));
  }
  const sk = await worker.db.exec("SELECT skill, COUNT(*) n FROM job_skills GROUP BY 1 ORDER BY n DESC");
  pickerValues.skill = (sk[0]?.values || []).map(([v, n]) => ({ value: v, label: `${v} (${fmtInt(n)})` }));
  const co = await worker.db.exec("SELECT domain, COALESCE(name, domain), open_jobs FROM companies WHERE open_jobs > 0 ORDER BY open_jobs DESC");
  pickerValues.company = (co[0]?.values || []).map(([d, name, n]) => ({ value: d, label: `${name} (${fmtInt(n)})` }));
}

// Builder UI.
function fillSelect(el, entries, current) {
  el.innerHTML = entries.map(([v, label]) => `<option value="${escapeHtml(v)}" ${v === current ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function renderBuilder() {
  fillSelect($("qb-status"), Object.entries(QB.STATUS).map(([k, v]) => [k, v.label]), state.status);
  fillSelect($("qb-group"), Object.entries(QB.GROUPS).map(([k, v]) => [k, v.label]), state.group);
  fillSelect($("qb-metric"), Object.entries(QB.METRICS).map(([k, v]) => [k, v.label]), state.metric);
  $("qb-metric-field").hidden = state.group === "none";
  $("qb-limit").value = state.limit;
  fillSelect($("qb-add-field"), [["", "Add a filter…"], ...Object.entries(QB.FIELDS).map(([k, v]) => [k, v.label])], "");
  renderFilters();
}

function renderFilters() {
  const host = $("qb-filters");
  filterWidgets.clear();
  host.innerHTML = (state.filters || []).map((f, i) => {
    const spec = QB.FIELDS[f.field];
    if (!spec) return "";
    let control;
    if (spec.kind === "pick") control = `<div class="ms qb-ms" data-i="${i}"></div>`;
    else if (spec.kind === "text") control = `<input type="text" class="qb-input" data-i="${i}" data-k="text" value="${escapeHtml(f.text || "")}" placeholder="contains…" />`;
    else control = `<input type="text" class="qb-input qb-date" data-i="${i}" data-k="from" value="${escapeHtml(f.from || "")}" placeholder="from YYYY-MM-DD" />
                    <input type="text" class="qb-input qb-date" data-i="${i}" data-k="to" value="${escapeHtml(f.to || "")}" placeholder="to YYYY-MM-DD" />`;
    return `<div class="qb-filter">
      <span class="qb-filter-name">${escapeHtml(spec.label)}</span>
      <span class="qb-filter-op">${spec.kind === "pick" ? "is one of" : spec.kind === "text" ? "contains" : "between"}</span>
      ${control}
      <button type="button" class="qb-remove" data-i="${i}" aria-label="Remove filter">&times;</button>
    </div>`;
  }).join("");

  host.querySelectorAll(".qb-ms").forEach((el) => {
    const i = Number(el.dataset.i);
    const f = state.filters[i];
    const spec = QB.FIELDS[f.field];
    filterWidgets.set(i, createMultiSelect(el, {
      placeholder: `Any ${spec.label.toLowerCase()}`,
      options: pickerValues[f.field] || [],
      searchable: !!spec.searchable || (pickerValues[f.field] || []).length > 20,
      selected: f.values || [],
      onChange: (vals) => { state.filters[i].values = vals; syncSqlFromBuilder(); },
    }));
  });
}

function syncSqlFromBuilder() {
  $("explore-sql").value = QB.buildSql(state);
}

function readBuilder() {
  // A control with no options yet has value "", and copying that into
  // state would replace a real default with nothing. Keep what we had.
  const pick = (id, current) => $(id).value || current;
  state.status = pick("qb-status", state.status);
  state.group = pick("qb-group", state.group);
  state.metric = pick("qb-metric", state.metric);
  state.limit = parseInt($("qb-limit").value, 10) || state.limit || 25;
  $("qb-filters").querySelectorAll("input.qb-input").forEach((inp) => {
    const f = state.filters[Number(inp.dataset.i)];
    if (f) f[inp.dataset.k] = inp.value;
  });
}

// Switching to SQL normally shows the query the builder wrote. A
// starter query or a shared link brings its own SQL instead; passing it
// here keeps the builder from writing over it.
function setMode(next, sql) {
  mode = next;
  document.querySelectorAll(".seg [data-mode]").forEach((b) => {
    const on = b.dataset.mode === next;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
  $("qb").hidden = next !== "builder";
  $("sqlmode").hidden = next !== "sql";
  $("qb-reset").hidden = next !== "builder";
  if (next !== "sql") renderBuilder();
  else if (sql != null) $("explore-sql").value = sql;
  else { readBuilder(); syncSqlFromBuilder(); }
}

function setViz(next) {
  viz = next;
  document.querySelectorAll(".seg [data-viz]").forEach((b) => {
    const on = b.dataset.viz === next;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
}

// URL state. The builder state when in builder mode, the SQL otherwise,
// so a link reproduces the view either way.
function writeUrl() {
  const p = new URLSearchParams();
  if (mode === "builder") p.set("b", QB.encodeState(state));
  else p.set("q", btoa(unescape(encodeURIComponent($("explore-sql").value))));
  if (viz !== "auto") p.set("v", viz);
  history.replaceState(null, "", `${location.pathname}?${p}`);
}

function readUrl() {
  const p = new URLSearchParams(location.search);
  if (p.get("v")) setViz(p.get("v"));
  if (p.get("b")) {
    const s = QB.decodeState(p.get("b"));
    if (s) { state = Object.assign(QB.defaultState(), s); return "builder"; }
  }
  if (p.get("q")) {
    try { $("explore-sql").value = decodeURIComponent(escape(atob(p.get("q")))); return "sql"; } catch { /* fall through */ }
  }
  return null;
}

// Running and rendering.
async function run() {
  if (!worker || running) return;
  if (mode === "builder") { readBuilder(); syncSqlFromBuilder(); }
  const sql = $("explore-sql").value.trim();
  if (!sql) return;
  running = true;
  const metric = $("explore-metric");
  const err = $("explore-error");
  err.hidden = true;
  metric.textContent = "Running…";
  document.body.classList.add("explore-busy");
  const t0 = performance.now();
  const before = await worker.worker.bytesRead;
  try {
    const result = await Promise.race([
      worker.db.exec(sql),
      new Promise((_, reject) => setTimeout(() => reject(new Error(
        `Still running after ${QUERY_TIMEOUT_MS / 1000}s. Narrow it with a filter or an indexed column.`)), QUERY_TIMEOUT_MS)),
    ]);
    const ms = Math.round(performance.now() - t0);
    const read = (await worker.worker.bytesRead) - before;
    writeUrl();
    render(result, ms, read);
  } catch (e) {
    // A query that timed out is still running inside the worker, and
    // every later query would queue behind it. A read the network
    // dropped leaves the runtime holding a page it should not trust,
    // which SQLite reports as a malformed file. Either way: throw the
    // worker away and open the database again, and say so in words a
    // reader can act on.
    const msg = String(e.message || e);
    const broken = /malformed|XMLHttpRequest|Failed to load/.test(msg);
    const timedOut = /Still running/.test(msg);
    err.textContent = timedOut
      ? `Stopped after ${QUERY_TIMEOUT_MS / 1000} s. Narrow it with a filter or an indexed column; the database has been reopened.`
      : broken ? "The network did not deliver every page this query needed. It has been reopened; run it again, or narrow it with a filter."
      : msg;
    if (timedOut || broken) await reopenDatabase();
    err.hidden = false;
    $("explore-result").innerHTML = "";
    metric.textContent = "Failed";
    lastResult = null;
    $("explore-csv").disabled = true;
  } finally {
    running = false;
    document.body.classList.remove("explore-busy");
  }
}

function render(result, ms, read) {
  const metric = $("explore-metric");
  const out = $("explore-result");
  if (!result.length) {
    const why = mode === "builder" ? "No listings match these filters." : "The query returned no rows.";
    out.innerHTML = `<div class="empty-state"><strong>No results</strong><span>${why}</span></div>`;
    metric.textContent = `0 rows in ${ms} ms, ${fmtBytes(read)} fetched`;
    lastResult = null;
    $("explore-csv").disabled = true;
    return;
  }
  const { columns, values } = result[result.length - 1];
  lastResult = { columns, values };
  $("explore-csv").disabled = false;
  const shown = values.slice(0, MAX_ROWS);
  const kind = pickViz(columns, shown);
  metric.textContent = `${fmtInt(values.length)} row${values.length === 1 ? "" : "s"} in ${ms} ms, ${fmtBytes(read)} fetched`
    + (values.length > MAX_ROWS ? `, showing ${fmtInt(MAX_ROWS)}` : "");
  if (kind === "bar") out.innerHTML = renderBars(columns, shown);
  else if (kind === "line") out.innerHTML = renderLine(columns, shown);
  else out.innerHTML = renderTable(columns, shown);
}

// Auto picks the shape the data is already in: a label and a number is
// bars, a date and a number is a line, anything else a table.
function pickViz(columns, rows) {
  if (viz !== "auto") return viz;
  if (columns.length < 2 || rows.length < 2 || rows.length > 60) return "table";
  const firstIsDate = rows.every((r) => /^\d{4}-(\d{2}|W\d{2})(-\d{2})?/.test(String(r[0] ?? "")));
  if (valueColumn(columns, rows) < 0) return "table";
  return firstIsDate ? "line" : "bar";
}

// The column a chart draws: the last one that is a number in every
// row. The builder's share metric returns the count and the share side
// by side, and the share, the thing that was asked for, is the last.
// A bare count, or a hand-written query, still gets its second column.
function valueColumn(columns, rows) {
  for (let i = columns.length - 1; i >= 1; i--) {
    if (rows.every((r) => typeof r[i] === "number")) return i;
  }
  return -1;
}

function renderTable(columns, rows) {
  const head = columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
  const body = rows.map((r) => `<tr>${r.map((v, i) => cell(v, columns[i])).join("")}</tr>`).join("");
  return `<div class="explore-table-wrap"><table class="explore-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function cell(v, col) {
  if (v == null) return '<td class="null">NULL</td>';
  if (typeof v === "number") return `<td class="num">${Number.isInteger(v) ? fmtInt(v) : v}</td>`;
  const s = String(v);
  if (col === "url" && /^https?:\/\//.test(s)) return `<td><a class="link" href="${escapeHtml(s)}" target="_blank" rel="noopener">open</a></td>`;
  return `<td>${escapeHtml(s)}</td>`;
}

// Bars. Label, track, value on one line each; the track column shares
// one scale from a baseline to the largest value, with gridlines at the
// quarters and their values ticked underneath. First row green, One
// Voice: the largest is the one thing the chart is saying.
function renderBars(columns, rows) {
  const vi = Math.max(1, valueColumn(columns, rows));
  const v = (r) => r[vi];
  const max = Math.max(1, ...rows.map((r) => Number(v(r)) || 0));
  const tick = (f) => (Number.isInteger(max) ? fmtInt(Math.round(max * f)) : (max * f).toFixed(1));
  return `<div class="xbars">${rows.map((r) => `
    <div class="xbar">
      <div class="xbar-name" title="${escapeHtml(r[0])}">${escapeHtml(r[0] ?? "NULL")}</div>
      <div class="xbar-track"><div class="xbar-fill" style="width:${((Number(v(r)) || 0) / max) * 100}%"></div></div>
      <div class="xbar-n">${Number.isInteger(v(r)) ? fmtInt(v(r)) : v(r)}</div>
    </div>`).join("")}
    <div class="xbar xbar-axis">
      <div class="xbar-name">${escapeHtml(columns[0])}</div>
      <div class="xbar-ticks">${[0, 0.25, 0.5, 0.75, 1].map((f) => `<span style="left:${f * 100}%">${tick(f)}</span>`).join("")}</div>
      <div class="xbar-n">${escapeHtml(columns[vi])}</div>
    </div>
  </div>`;
}

// Line. One stroke, the homepage's trend voice, on a scale with the
// quarters ruled and labelled. The svg stretches to the panel, so the
// labels live outside it in HTML and stay crisp.
function renderLine(columns, rows) {
  const w = 640, h = 160, pad = 2;
  const vi = Math.max(1, valueColumn(columns, rows));
  const ys = rows.map((r) => Number(r[vi]) || 0);
  const max = Math.max(1, ...ys);
  const step = rows.length > 1 ? (w - pad * 2) / (rows.length - 1) : 0;
  const yOf = (y) => (h - pad - (y / max) * (h - pad * 2)).toFixed(1);
  const d = ys.map((y, i) => `${i ? "L" : "M"}${(pad + i * step).toFixed(1)},${yOf(y)}`).join(" ");
  const tick = (f) => (Number.isInteger(max) ? fmtInt(Math.round(max * f)) : (max * f).toFixed(1));
  const mid = rows[Math.floor(rows.length / 2)][0];
  return `<div class="xline">
    <div class="xline-y">${[1, 0.75, 0.5, 0.25, 0].map((f) => `<span>${tick(f)}</span>`).join("")}</div>
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" class="xline-svg" aria-label="${escapeHtml(columns[vi])} over ${escapeHtml(columns[0])}">
      ${[1, 0.75, 0.5, 0.25].map((f) => `<line class="xline-grid" x1="0" x2="${w}" y1="${yOf(max * f)}" y2="${yOf(max * f)}"/>`).join("")}
      <line class="xline-base" x1="0" x2="${w}" y1="${h - pad}" y2="${h - pad}"/>
      <path class="xline-path" d="${d}"><title>${escapeHtml(columns[vi])}</title></path>
    </svg>
    <div class="xline-x"><span>${escapeHtml(rows[0][0])}</span><span>${escapeHtml(mid)}</span><span>${escapeHtml(rows[rows.length - 1][0])}</span></div>
  </div>`;
}

// CSV of the whole last answer, not just the rows on screen. Quotes
// anything with a comma, quote, or newline; NULL becomes an empty cell.
function exportCsv() {
  if (!lastResult) return;
  const q = (v) => {
    if (v == null) return "";
    const s = String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [lastResult.columns.map(q).join(",")].concat(lastResult.values.map((r) => r.map(q).join(",")));
  const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `opentechjobs-explore-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// Sidebar.
function renderTemplates() {
  $("explore-templates").innerHTML = TEMPLATES.map((t, i) =>
    `<button type="button" class="explore-template" data-i="${i}">${escapeHtml(t.name)}</button>`).join("");
  $("explore-templates").addEventListener("click", (e) => {
    const b = e.target.closest(".explore-template");
    if (!b) return;
    document.querySelectorAll(".explore-template.active").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    const t = TEMPLATES[Number(b.dataset.i)];
    setViz("auto");
    if (t.state) { state = Object.assign(QB.defaultState(), JSON.parse(JSON.stringify(t.state))); setMode("builder"); }
    else setMode("sql", t.sql);
    run();
  });
}

// The schema explorer is a hand-built tree, not a <details>, so it
// takes the system's own marker and spacing rather than the browser's.
// Typing in the search box filters columns by name or note across every
// table, opens the tables that still have something, and hides the rest.
function renderSchema() {
  $("explore-schema").innerHTML = SCHEMA.map((t, i) => `
    <div class="sx-table ${i === 0 ? "open" : ""}" data-table="${t.table}">
      <button type="button" class="sx-toggle" aria-expanded="${i === 0}">
        <code>${t.table}</code><span class="sx-count">${t.columns.length}</span><span class="sx-note">${escapeHtml(t.note)}</span>
      </button>
      <ul ${i === 0 ? "" : "hidden"}>${t.columns.map(([c, ty, n]) => `
        <li data-col="${escapeHtml(c)}" data-note="${escapeHtml(n.toLowerCase())}"><code>${c}</code><span class="sx-type">${ty}</span>${n ? `<span class="sx-note">${escapeHtml(n)}</span>` : ""}</li>`).join("")}</ul>
    </div>`).join("");
  $("explore-schema").addEventListener("click", (e) => {
    const b = e.target.closest(".sx-toggle");
    if (!b) return;
    const box = b.parentElement;
    const ul = box.querySelector("ul");
    const open = ul.hidden;
    ul.hidden = !open;
    box.classList.toggle("open", open);
    b.setAttribute("aria-expanded", String(open));
  });
  $("schema-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll(".sx-table").forEach((box, i) => {
      const ul = box.querySelector("ul");
      let hits = 0;
      ul.querySelectorAll("li").forEach((li) => {
        const hit = !q || li.dataset.col.includes(q) || li.dataset.note.includes(q) || box.dataset.table.includes(q);
        li.hidden = !hit;
        if (hit) hits++;
      });
      box.hidden = Boolean(q) && hits === 0;
      const open = q ? hits > 0 : i === 0;
      ul.hidden = !open;
      box.classList.toggle("open", open);
      box.querySelector(".sx-toggle").setAttribute("aria-expanded", String(open));
    });
  });
}

function wireThemeToggle() {
  const btn = $("theme-toggle");
  const sync = () => { btn.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "Light" : "Dark"; };
  sync();
  btn.addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    if (next === "dark") root.setAttribute("data-theme", "dark"); else root.removeAttribute("data-theme");
    localStorage.setItem("iljobs_theme", next);
    sync();
  });
}

async function boot() {
  wireThemeToggle();
  renderTemplates();
  renderSchema();

  document.querySelectorAll(".seg [data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  document.querySelectorAll(".seg [data-viz]").forEach((b) => b.addEventListener("click", () => { setViz(b.dataset.viz); run(); }));
  $("explore-run").addEventListener("click", run);
  $("explore-csv").addEventListener("click", exportCsv);
  $("qb-reset").addEventListener("click", () => { state = QB.defaultState(); renderBuilder(); run(); });
  ["qb-status", "qb-group", "qb-metric"].forEach((id) => $(id).addEventListener("change", () => {
    readBuilder();
    $("qb-metric-field").hidden = state.group === "none";
    run();
  }));
  $("qb-limit").addEventListener("change", run);
  $("qb-add-field").addEventListener("change", (e) => {
    const field = e.target.value;
    if (!field) return;
    state.filters.push({ field, values: [], text: "", from: "", to: "" });
    e.target.value = "";
    renderFilters();
  });
  $("qb-filters").addEventListener("click", (e) => {
    const b = e.target.closest(".qb-remove");
    if (!b) return;
    state.filters.splice(Number(b.dataset.i), 1);
    renderFilters();
    run();
  });
  $("qb-filters").addEventListener("change", (e) => { if (e.target.matches("input.qb-input")) run(); });
  $("explore-sql").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); run(); }
  });
  $("explore-share").addEventListener("click", async () => {
    writeUrl();
    try {
      await navigator.clipboard.writeText(location.href);
      $("explore-share").textContent = "Copied";
      setTimeout(() => { $("explore-share").textContent = "Copy link"; }, 1500);
    } catch { /* the URL bar already has it */ }
  });

  try {
    await openDatabase();
  } catch (e) {
    $("explore-status").textContent = "The database runtime could not start.";
    $("explore-error").textContent = `${e.message || e}. This page needs WebAssembly and a modern browser.`;
    $("explore-error").hidden = false;
    return;
  }

  const fromUrl = readUrl();
  if (fromUrl === "sql") setMode("sql", $("explore-sql").value);
  else {
    if (!fromUrl) { state = Object.assign(QB.defaultState(), JSON.parse(JSON.stringify(TEMPLATES[0].state))); document.querySelector(".explore-template").classList.add("active"); }
    setMode("builder");
  }
  // The first answer first. Picker values load behind it and any
  // multi-select already on screen picks them up on its next render.
  await run();
  await loadPickerValues();
  if (mode === "builder") renderFilters();
}

boot();
