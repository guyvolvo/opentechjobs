import { chromium } from "playwright";
const b = await chromium.launch();
const t = "x." + Buffer.from(JSON.stringify({ email: "guyvoloshin@gmail.com", exp: Math.floor(Date.now() / 1000) + 86400 })).toString("base64url") + ".y";
for (const [name, storage] of [["bar-out", {}], ["bar-in", { iljobs_auth_tokens: JSON.stringify({ id_token: t }) }]]) {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
  const page = await ctx.newPage();
  await page.addInitScript((kv) => { try { for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); } catch {} }, storage);
  await page.goto("http://127.0.0.1:8000/", { waitUntil: "load" });
  await page.waitForTimeout(1200);
  console.log(name, JSON.stringify(await page.evaluate(() => {
    const f = (s) => { const e = document.querySelector(s); return e ? getComputedStyle(e).fontSize : null; };
    return { headerText: f(".hero-bar .hero-bar-name"), footerText: f(".hero-foot"), control: f(".hero-account-btn") };
  })));
  await page.screenshot({ path: `tests/e2e/${name}.png`, clip: { x: 0, y: 0, width: 1440, height: 110 } });
  await ctx.close();
}
await b.close();
