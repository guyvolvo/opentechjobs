import { chromium } from "playwright";
const b = await chromium.launch();
const errs = [];
// The landing dialog's Send code, all the way to a network request.
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
const page = await ctx.newPage();
page.on("pageerror", (e) => errs.push("landing: " + e.message));
const calls = [];
await page.route("**/api/auth/email/start", async (route) => {
  calls.push(JSON.parse(route.request().postData() || "{}"));
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ session: "fake-session" }) });
});
await page.goto("http://127.0.0.1:8000/", { waitUntil: "load" });
await page.waitForTimeout(1000);
await page.click("#hero-signin");
await page.fill("#hero-email-input", "guyvoloshin@gmail.com");
await page.click("#hero-email-form button");
await page.waitForTimeout(1500);
console.log("request sent:", JSON.stringify(calls), "| error shown:", (await page.textContent("#hero-auth-error")).trim() || "(none)");
console.log("code step shown:", !(await page.locator("#hero-otp-form").isHidden()));
await ctx.close();
await b.close();
console.log(errs.length ? "PAGE ERRORS: " + errs.join(" | ") : "no page errors");
