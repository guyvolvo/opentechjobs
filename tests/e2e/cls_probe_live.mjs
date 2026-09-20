// The live site as different visitors: where does the jobs table still move?
// Run: node tests/e2e/cls_probe_live.mjs
import { chromium, devices } from "playwright";

const origin = "https://opentechjobs.org";
const b = await chromium.launch();

const OBSERVE = `
  window.__shifts = [];
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) {
      if (e.hadRecentInput) continue;
      const src = (e.sources || []).map((s) => {
        const n = s.node;
        const name = n && n.nodeType === 1
          ? n.tagName.toLowerCase() + (n.id ? "#" + n.id : "") + (n.className && typeof n.className === "string" ? "." + n.className.trim().split(/\s+/).slice(0, 2).join(".") : "")
          : "(text)";
        const r = s.previousRect, c = s.currentRect;
        return name + " " + Math.round(r.y) + "->" + Math.round(c.y) + " h" + Math.round(r.height) + "->" + Math.round(c.height);
      });
      window.__shifts.push({ t: Math.round(e.startTime), v: +e.value.toFixed(4), src });
    }
  }).observe({ type: "layout-shift", buffered: true });
`;

async function run(name, ctxOpts, { slow = false, storage = {}, path = "/board" } = {}) {
  const ctx = await b.newContext({ ...ctxOpts, serviceWorkers: "block" });
  const page = await ctx.newPage();
  if (slow) {
    const cdp = await ctx.newCDPSession(page);
    await cdp.send("Network.enable");
    await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 400, downloadThroughput: 200 * 1024, uploadThroughput: 100 * 1024 });
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  }
  await page.addInitScript(OBSERVE);
  await page.addInitScript((kv) => { try { for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); } catch {} }, storage);
  await page.goto(origin + path, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#jobs-body tr[data-id], #jobs-body .empty, #jobs-body td", { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(slow ? 15000 : 8000);
  const shifts = await page.evaluate(() => window.__shifts);
  const total = shifts.reduce((a, s) => a + s.v, 0);
  const rows = await page.evaluate(() => document.querySelectorAll("#jobs-body tr[data-id]").length);
  console.log(`\n== ${name}${slow ? " (slow 3G, 4x CPU)" : ""}  rows=${rows}`);
  console.log(`   ${shifts.length} shifts, CLS ${total.toFixed(3)}`);
  for (const s of shifts.filter((s) => s.v >= 0.002)) console.log(`     t=${s.t}ms  +${s.v}  ${s.src.slice(0, 3).join(" | ")}`);
  await ctx.close();
}

const desk = { viewport: { width: 1400, height: 900 } };
const phone = { ...devices["iPhone 13"] };
await run("desktop, new visitor (geo prompt shows)", desk);
await run("desktop, returning", desk, { storage: { iljobs_geo_asked: "1" } });
await run("desktop, dark + stats collapsed", desk, { storage: { iljobs_geo_asked: "1", iljobs_theme: "dark", iljobs_stats_collapsed: "1" } });
await run("desktop, saved filters (rare search)", desk, { storage: { iljobs_geo_asked: "1", iljobs_filters: JSON.stringify({ q: "zzzzqqq" }) } });
await run("desktop, ?company= link", desk, { storage: { iljobs_geo_asked: "1" }, path: "/board?company=wix.com" });
await run("phone, new visitor", phone);
await run("phone, returning", phone, { storage: { iljobs_geo_asked: "1" } });
await run("phone, returning", phone, { storage: { iljobs_geo_asked: "1" }, slow: true });
await run("desktop, returning", desk, { storage: { iljobs_geo_asked: "1" }, slow: true });
await b.close();
