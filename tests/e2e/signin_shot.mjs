import { chromium } from "playwright";
const b = await chromium.launch();
const shots = [
  ["signin-landing-bar", "/", { x: 1000, y: 0, width: 440, height: 90 }],
  ["signin-landing-foot", "/", null],
  ["signin-account", "/account", null],
];
for (const [name, path, clip] of shots) {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log(name, "pageerror:", e.message));
  await page.goto("http://127.0.0.1:8000" + path, { waitUntil: "load" });
  await page.waitForTimeout(1500);
  if (name === "signin-landing-foot") await page.evaluate(() => document.querySelector(".hero-foot").scrollIntoView());
  await page.waitForTimeout(400);
  await page.screenshot({ path: `tests/e2e/${name}.png`, ...(clip ? { clip } : {}) });
  if (name === "signin-landing-bar") {
    const m = await page.evaluate(() => { const h = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().height) : null; }; return { stack: h(".hero-bar-stack"), signin: h(".hero-account-btn"), foot: getComputedStyle(document.querySelector(".hero-foot")).fontSize }; });
    console.log(name, JSON.stringify(m));
  }
  if (name === "signin-account") console.log("account signed-out block:", (await page.textContent("#account-signedout")).trim().replace(/\s+/g, " ").slice(0, 110));
  await ctx.close();
}
await b.close();
