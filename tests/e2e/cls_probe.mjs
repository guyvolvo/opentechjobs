// Layout shifts on the live board and landing, attributed to the element
// that moved and the moment it moved. Run: node tests/e2e/cls_probe.mjs [origin]
import { chromium, devices } from "playwright";

const origin = process.argv[2] || "https://opentechjobs.org";
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

async function probe(name, ctxOpts, path) {
  const ctx = await b.newContext({ ...ctxOpts, serviceWorkers: "block" });
  const page = await ctx.newPage();
  await page.addInitScript(OBSERVE);
  await page.addInitScript(() => { try { localStorage.setItem("iljobs_geo_asked", "1"); } catch {} });
  await page.goto(origin + path, { waitUntil: "load" });
  await page.waitForTimeout(6000);
  const shifts = await page.evaluate(() => window.__shifts);
  const total = shifts.reduce((a, s) => a + s.v, 0);
  console.log(`\n== ${name} ${path}: ${shifts.length} shifts, CLS ${total.toFixed(3)}`);
  for (const s of shifts.filter((s) => s.v >= 0.005)) console.log(`  t=${s.t}ms  +${s.v}  ${s.src.slice(0, 3).join(" | ")}`);
  await ctx.close();
}

for (const path of ["/board", "/"]) {
  await probe("desktop", { viewport: { width: 1400, height: 900 } }, path);
  await probe("phone", { ...devices["iPhone 13"] }, path);
}
await b.close();
