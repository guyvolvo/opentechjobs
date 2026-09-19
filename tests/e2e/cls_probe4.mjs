// The local frontend against the live API: are the skeletons the real
// heights now, and does a slow first load still shift anything?
// Serves board.html, style.css and app.js from the working copy; every
// other request goes to the live origin.
// Run: node tests/e2e/cls_probe4.mjs
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
        const name = n && n.nodeType === 1
          ? n.tagName.toLowerCase() + (n.id ? "#" + n.id : "") + (n.className && typeof n.className === "string" ? "." + n.className.trim().split(/\\s+/).slice(0, 2).join(".") : "")
          : "(text)";
        const r = s.previousRect, c = s.currentRect;
        return name + " " + Math.round(r.y) + "->" + Math.round(c.y) + " h" + Math.round(r.height) + "->" + Math.round(c.height);
      });
      window.__shifts.push({ t: Math.round(e.startTime), v: +e.value.toFixed(4), src });
    }
  }).observe({ type: "layout-shift", buffered: true });
`;

async function run(name, ctxOpts, slow) {
  const ctx = await b.newContext({ ...ctxOpts, serviceWorkers: "block" });
  const page = await ctx.newPage();
  await page.route(`${origin}/**`, async (route) => {
    const u = new URL(route.request().url());
    const hit = LOCAL[u.pathname];
    if (hit) return route.fulfill({ status: 200, contentType: hit[1], body: await readFile(hit[0]) });
    return route.continue();
  });
  if (slow) {
    const cdp = await ctx.newCDPSession(page);
    await cdp.send("Network.enable");
    await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 400, downloadThroughput: 200 * 1024, uploadThroughput: 100 * 1024 });
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  }
  await page.addInitScript(OBSERVE);
  await page.addInitScript(() => { try { localStorage.setItem("geo-prompt-answered", "1"); } catch {} });
  await page.goto(origin + "/board", { waitUntil: "domcontentloaded" });
  // first-frame skeleton heights, before app.js has replaced them
  const first = await page.evaluate(() => {
    const h = (sel) => { const el = document.querySelector(sel); return el ? Math.round(el.getBoundingClientRect().height) : null; };
    return { skRows: document.querySelectorAll("#jobs-body .skeleton-row").length, skRow: h("#jobs-body .skeleton-row"),
             metricsGrid: h("#metrics-grid"), scopedPanel: h("#scoped-panel-grid .sk-panel"),
             statsTop: Math.round(document.getElementById("stats-board").getBoundingClientRect().top) };
  });
  await page.waitForSelector("#jobs-body tr[data-id]", { timeout: 60000 });
  await page.waitForTimeout(slow ? 12000 : 6000);
  const after = await page.evaluate(() => {
    const h = (sel) => { const el = document.querySelector(sel); return el ? Math.round(el.getBoundingClientRect().height) : null; };
    const rows = [...document.querySelectorAll("#jobs-body tr[data-id]")];
    const avg = rows.slice(0, 20).reduce((a, r) => a + r.getBoundingClientRect().height, 0) / Math.min(20, rows.length);
    const tb = document.getElementById("jobs-body");
    const tmp = document.createElement("tbody"); tmp.innerHTML = jobsSkeletonHtml(1); tb.parentNode.appendChild(tmp);
    const sk = Math.round(tmp.firstElementChild.getBoundingClientRect().height); tmp.remove();
    return { rowAvg: Math.round(avg), skRow: sk, metricsGrid: h("#metrics-grid"), metricCards: document.querySelectorAll("#metrics-grid .metric-card").length,
             scopedPanel: h("#scoped-panel-grid .panel"), pipeline: h("#pipeline-grid"), panelGrid: h("#panel-grid") };
  });
  const shifts = await page.evaluate(() => window.__shifts);
  const total = shifts.reduce((a, s) => a + s.v, 0);
  console.log(`\n== ${name}${slow ? " (slow 3G, 4x CPU)" : ""}`);
  console.log("   first frame:", JSON.stringify(first));
  console.log("   after load: ", JSON.stringify(after));
  console.log(`   ${shifts.length} shifts, CLS ${total.toFixed(3)}`);
  for (const s of shifts.filter((s) => s.v >= 0.003)) console.log(`     t=${s.t}ms  +${s.v}  ${s.src.slice(0, 3).join(" | ")}`);
  await ctx.close();
}

await run("desktop", { viewport: { width: 1400, height: 900 } }, false);
await run("phone", { ...devices["iPhone 13"] }, false);
await run("phone", { ...devices["iPhone 13"] }, true);
await b.close();
