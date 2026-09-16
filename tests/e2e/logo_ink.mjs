// Contrast of each mark's own ink against the page, ignoring the empty
// background inside its box. The earlier sampler took the pixel furthest
// from mid-grey, which for a pale logo was the paper around it: it
// measured the box, not the mark.
import { chromium } from "@playwright/test";
import { spawn } from "node:child_process";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8846", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);

const lum = ([r, g, b]) => {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
};
const ratio = (a, b) => { const [lo, hi] = [lum(a), lum(b)].sort((x, y) => x - y); return (hi + 0.05) / (lo + 0.05); };

const stats = await (await fetch("https://opentechjobs.org/stats.json")).json();
const browser = await chromium.launch();
for (const theme of ["light", "dark"]) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  await context.addInitScript(({ t, s }) => {
    try { localStorage.setItem("iljobs_theme", t); } catch {}
    const real = window.fetch.bind(window);
    window.fetch = (i, init) => String(i && i.url ? i.url : i).includes("stats.json")
      ? Promise.resolve(new Response(JSON.stringify(s), { status: 200, headers: { "Content-Type": "application/json" } }))
      : real(i, init);
  }, { t: theme, s: stats });
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:8846/hero.html", { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForTimeout(3500);
  await page.evaluate(() => document.querySelectorAll(".hero-ticker-track").forEach((el) => { el.style.animationPlayState = "paused"; }));
  await page.waitForTimeout(200);

  const rowBox = await page.locator(".hero-logos").boundingBox();
  const meta = await page.evaluate(() => ({
    bg: getComputedStyle(document.querySelector(".hero-logos")).backgroundColor,
    filter: getComputedStyle(document.querySelector(".hero-logo img")).filter,
    opacity: getComputedStyle(document.querySelector(".hero-logo img")).opacity,
    marks: [...document.querySelectorAll(".hero-logo img")]
      .map((im) => ({ r: im.getBoundingClientRect(), src: im.src }))
      .filter((m) => m.r.left > 40 && m.r.right < innerWidth - 40)
      .slice(0, 10)
      .map((m) => ({ x: m.r.x, y: m.r.y, w: m.r.width, h: m.r.height, src: m.src.slice(-24) })),
  }));
  const shot = await page.screenshot({ clip: { x: 0, y: rowBox.y, width: 1440, height: Math.ceil(rowBox.height) } });
  const out = await page.evaluate(async ({ b64, marks, rowTop, bg }) => {
    const img = new Image(); img.src = "data:image/png;base64," + b64; await img.decode();
    const c = document.createElement("canvas"); c.width = img.width; c.height = img.height;
    const g = c.getContext("2d"); g.drawImage(img, 0, 0);
    const dpr = img.width / 1440;
    const bgpx = bg.match(/\d+/g).slice(0, 3).map(Number);
    const near = (p) => Math.abs(p[0] - bgpx[0]) + Math.abs(p[1] - bgpx[1]) + Math.abs(p[2] - bgpx[2]) < 30;
    return marks.map((m) => {
      const d = g.getImageData(Math.round(m.x * dpr), Math.round((m.y - rowTop) * dpr),
                               Math.round(m.w * dpr), Math.round(m.h * dpr)).data;
      // Ink = pixels that differ from the page. Report the median of them,
      // so one stray antialiased pixel cannot speak for the whole mark.
      const ink = [];
      for (let i = 0; i < d.length; i += 4) {
        if (d[i + 3] < 8) continue;
        const px = [d[i], d[i + 1], d[i + 2]];
        if (!near(px)) ink.push(px);
      }
      if (!ink.length) return { src: m.src, ink: null, coverage: 0 };
      ink.sort((a, b) => (a[0] + a[1] + a[2]) - (b[0] + b[1] + b[2]));
      return { src: m.src, ink: ink[Math.floor(ink.length / 2)], coverage: Math.round(100 * ink.length / (d.length / 4)) };
    });
  }, { b64: shot.toString("base64"), marks: meta.marks, rowTop: rowBox.y, bg: meta.bg });

  const bg = meta.bg.match(/\d+/g).slice(0, 3).map(Number);
  console.log(`\n${theme}: page ${meta.bg}  filter "${meta.filter}"  opacity ${meta.opacity}`);
  const rs = [];
  for (const o of out) {
    if (!o.ink) { console.log(`  ${o.src.padEnd(26)} no ink found`); continue; }
    const r = ratio(o.ink, bg); rs.push(r);
    console.log(`  ${o.src.padEnd(26)} median ink ${String(o.ink).padEnd(15)} ${o.coverage}% of box  ratio ${r.toFixed(2)}`);
  }
  rs.sort((a, b) => a - b);
  console.log(`  -> worst ${rs[0]?.toFixed(2)}  median ${rs[Math.floor(rs.length / 2)]?.toFixed(2)}  under 3:1 ${rs.filter((r) => r < 3).length}/${rs.length}`);
  await context.close();
}
await browser.close();
server.kill();
