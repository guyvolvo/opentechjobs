import { chromium } from "playwright";
const b = await chromium.launch();
const t = "x." + Buffer.from(JSON.stringify({ email: "guyvoloshin@gmail.com" })).toString("base64url") + ".y";
for (const [name, storage] of [["signed-out", {}], ["signed-in", { iljobs_auth_tokens: JSON.stringify({ id_token: t }) }]]) {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log(name, "pageerror:", e.message));
  await page.addInitScript((kv) => { try { for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); } catch {} }, storage);
  await page.goto("http://127.0.0.1:8000/account", { waitUntil: "load" });
  await page.waitForTimeout(2500);
  console.log(name, JSON.stringify(await page.evaluate(() => ({
    signinVisible: !document.querySelector("#account-signedout")?.hidden,
    bodyVisible: !!document.querySelector("#account-body")?.offsetParent,
    email: document.getElementById("account-email")?.textContent || "",
  }))));
  await ctx.close();
}
await b.close();
