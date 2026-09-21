import { chromium } from "playwright";
const b = await chromium.launch();
const errs = [];
const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: "block" });
const page = await ctx.newPage();
page.on("pageerror", (e) => errs.push(e.message));
await page.goto("http://127.0.0.1:8000/", { waitUntil: "load" });
await page.waitForFunction(() => /posted/.test(document.getElementById("trend-summary")?.textContent || ""), null, { timeout: 45000 });
await page.waitForTimeout(600);
console.log("summary:", (await page.textContent("#trend-summary")).trim().replace(/\s+/g, " ").slice(0, 120));
console.log("order:", await page.evaluate(() => {
  const el = document.querySelector(".hero-trend");
  const shot = document.querySelector(".hero-showcase");
  const feat = document.querySelector(".hero-features");
  return { afterImage: shot.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING ? true : false,
           beforeFeatures: el.compareDocumentPosition(feat) & Node.DOCUMENT_POSITION_FOLLOWING ? true : false };
}));
await page.evaluate(() => document.querySelector(".hero-trend").scrollIntoView({ block: "center" }));
await page.waitForTimeout(500);
await page.screenshot({ path: "tests/e2e/landing-trend.png" });
await ctx.close();
await b.close();
console.log(errs.length ? "PAGE ERRORS: " + errs.join(" | ") : "no page errors");
