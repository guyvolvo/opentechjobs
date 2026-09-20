// Working copy against the live API: the skills panel stamped before
// app.js, and the empty result standing in the table's place.
// Run: node tests/e2e/cls_probe5.mjs
import { chromium, devices } from "playwright";
import { readFile } from "node:fs/promises";

const origin = "https://opentechjobs.org";
const LOCAL = { "/board": ["frontend/board.html", "text/html"], "/style.css": ["frontend/style.css", "text/css"], "/app.js": ["frontend/app.js", "text/javascript"] };
const b = await chromium.launch();
const OBSERVE = `
  window.__shifts = [];
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) {
      if (e.hadRecentInput) continue;
      const src = (e.sources || []).map((s) => {
        const n = s.node;
        const name = n && n.nodeType === 1 ? n.tagName.toLowerCase() + (n.id ? "#" + n.id : "") + (typeof n.className === "string" && n.className ? "." + n.className.trim().split(/\s+/).slice(0, 2).join(".") : "") : "(text)";
        const r = s.previousRect, c = s.currentRect;
        return name + " " + Math.round(r.y) + "->" + Math.round(c.y) + " h" + Math.round(r.height) + "->" + Math.round(c.height);
      });
      window.__shifts.push({ t: Math.round(e.startTime), v: +e.value.toFixed(4), src });
    }
  }).observe({ type: "layout-shift", buffered: true });
`;
async function run(name, ctxOpts, storage, shot) {
  const ctx = await b.newContext({ ...ctxOpts, serviceWorkers: "block" });
  const page = await ctx.newPage();
  await page.route(`${origin}/**`, async (route) => {
    const hit = LOCAL[new URL(route.request().url()).pathname];
    if (hit) return route.fulfill({ status: 200, contentType: hit[1], body: await readFile(hit[0]) });
    return route.continue();
  });
  await page.addInitScript(OBSERVE);
  await page.addInitScript((kv) => { try { for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); } catch {} }, { iljobs_geo_asked: "1", ...storage });
  await page.goto(origin + "/board", { waitUntil: "domcontentloaded" });
  const first = await page.evaluate(() => { const p = document.getElementById("match-panel"); return { panelH: p.hidden ? 0 : Math.round(p.getBoundingClientRect().height), tableTop: Math.round(document.querySelector("table.jobs").getBoundingClientRect().top) }; });
  await page.waitForTimeout(7000);
  const after = await page.evaluate(() => { const p = document.getElementById("match-panel"); const t = document.querySelector("table.jobs"); return { panelH: p.hidden ? 0 : Math.round(p.getBoundingClientRect().height), tableTop: Math.round(t.getBoundingClientRect().top), tableShown: t.style.display !== "none", rows: document.querySelectorAll("#jobs-body tr[data-id]").length }; });
  const shifts = await page.evaluate(() => window.__shifts);
  console.log(`\n== ${name}  first=${JSON.stringify(first)}  after=${JSON.stringify(after)}`);
  console.log(`   ${shifts.length} shifts, CLS ${shifts.reduce((a, s) => a + s.v, 0).toFixed(3)}`);
  for (const s of shifts.filter((s) => s.v >= 0.002)) console.log(`     t=${s.t}ms  +${s.v}  ${s.src.slice(0, 3).join(" | ")}`);
  if (shot) await page.screenshot({ path: `tests/e2e/${shot}.png` });
  await ctx.close();
}
const desk = { viewport: { width: 1400, height: 900 } };
const phone = { ...devices["iPhone 13"] };
const skills = JSON.stringify({ skills: ["Python", "AWS", "Kubernetes", "React", "PostgreSQL", "Terraform", "Go", "Docker"], sort: "match", dir: "asc" });
await run("desktop, skills closed", desk, { iljobs_filters: skills });
await run("desktop, skills open", desk, { iljobs_filters: skills, iljobs_match_skills_open: "1" }, "board-skills-open");
await run("phone, skills open", phone, { iljobs_filters: skills, iljobs_match_skills_open: "1" });
await run("desktop, empty search", desk, { iljobs_filters: JSON.stringify({ search: "zzzzqqq" }) }, "board-empty");
await run("phone, empty search", phone, { iljobs_filters: JSON.stringify({ search: "zzzzqqq" }) });
await run("desktop, plain", desk, {});
await b.close();
