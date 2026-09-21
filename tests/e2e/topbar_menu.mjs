import { chromium } from "playwright";
const b = await chromium.launch();
const t = "x." + Buffer.from(JSON.stringify({ email: "guyvoloshin@gmail.com", exp: Math.floor(Date.now() / 1000) + 86400 })).toString("base64url") + ".y";
for (const [name, storage] of [["topbar-signed-in", { iljobs_auth_tokens: JSON.stringify({ id_token: t }) }], ["topbar-signed-out", {}]]) {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log(name, "pageerror:", e.message));
  await page.addInitScript((kv) => { try { localStorage.setItem("iljobs_geo_asked", "1"); for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); } catch {} }, storage);
  await page.goto("http://127.0.0.1:8000/board", { waitUntil: "load" });
  await page.waitForTimeout(2500);
  console.log(name, "| nav:", (await page.textContent(".topnav")).trim().replace(/\s+/g, " ").slice(0, 80));
  await page.click(name === "topbar-signed-in" ? "#topbar-account-btn" : "#auth-trigger");
  await page.waitForTimeout(400);
  const menu = name === "topbar-signed-in" ? "#topbar-menu" : "#auth-panel";
  console.log(name, "| menu:", (await page.textContent(menu)).trim().replace(/\s+/g, " ").slice(0, 120));
  await page.screenshot({ path: `tests/e2e/${name}.png`, clip: { x: 940, y: 0, width: 500, height: 420 } });
  if (name === "topbar-signed-in") {
    await page.click("#topbar-alert-btn");
    await page.waitForTimeout(600);
    console.log(name, "| alerts panel open:", !(await page.locator("#auth-panel").isHidden()), "| menu closed:", await page.locator("#topbar-menu").isHidden());
  }
  await ctx.close();
}
await b.close();
