import { chromium } from "playwright";
const b = await chromium.launch();
for (const [name, theme] of [["signin-dialog-light", "light"], ["signin-dialog-dark", "dark"]]) {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 800 }, serviceWorkers: "block" });
  const page = await ctx.newPage();
  await page.addInitScript((t) => { try { localStorage.setItem("iljobs_theme", t); } catch {} }, theme);
  await page.goto("http://127.0.0.1:8000/account", { waitUntil: "load" });
  await page.waitForTimeout(2200);
  console.log(name, JSON.stringify(await page.evaluate(() => {
    const d = document.querySelector(".signin-dialog").getBoundingClientRect();
    const sec = document.querySelector(".workspace > .section");
    return { centeredX: Math.round(d.left + d.width / 2) === Math.round(innerWidth / 2), centeredY: Math.abs((d.top + d.height / 2) - innerHeight / 2) < 3,
             sectionRule: getComputedStyle(sec).borderBottomWidth, titleShown: !!document.querySelector(".account-page > .section-title")?.offsetParent,
             lede: document.body.textContent.includes("No password to remember") };
  })));
  await page.screenshot({ path: `tests/e2e/${name}.png` });
  await ctx.close();
}
await b.close();
