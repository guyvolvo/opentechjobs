// The board's topbar alerts panel, which is the half of renderAuthState
// the account page must not get. It carries #alerts-list and the whole
// create form, and the account page owns those ids itself, so the panel
// is now rendered only where #auth-area does not say data-menu-only.
// This checks the board still gets it, and gets exactly one of each id.
//
//   node board_alerts_panel.mjs
import { chromium } from "playwright";
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const now = Math.floor(Date.now() / 1000);
const TOK = [b64({ alg: "RS256", typ: "JWT" }), b64({ sub: "x", email: "a@b.c", exp: now + 3600 }), "sig"].join(".");
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errs = [];
page.on("pageerror", (e) => errs.push(e.message));
page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
await page.addInitScript((t) => {
  localStorage.setItem("iljobs_auth_tokens", JSON.stringify({ id_token: t, access_token: t, refresh_token: "r" }));
  localStorage.setItem("iljobs_geo_asked", "1");
}, TOK);
const LOCAL = ["app", "auth", "signin_dialog", "mobile_nav"];
await page.route((u) => LOCAL.some((n) => u.pathname === `/${n}.js`), (r) =>
  r.fulfill({ path: `../../frontend${new URL(r.request().url()).pathname}`, contentType: "application/javascript" }));
await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
await page.route(/\/board(\?|$)/, (r) => r.fulfill({ path: "../../frontend/board.html", contentType: "text/html" }));
const json = (r, b) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
await page.route((u) => u.pathname === "/api/me/alerts", (r) => json(r, { alerts: [
  { alert_id: "a1", active: true, filter: { search: "sre", country: "IL" } }] }));
await page.route((u) => u.pathname === "/api/me/saved", (r) => json(r, { saved: [] }));
await page.goto("https://opentechjobs.org/board", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(2500);
await page.click("#topbar-account-btn");
await page.click("#topbar-alert-btn");
await page.waitForTimeout(600);
console.log(JSON.stringify(await page.evaluate(() => ({
  alertsListCount: document.querySelectorAll('[id="alerts-list"]').length,
  signoutCount: document.querySelectorAll('[id="auth-signout"]').length,
  panelOpen: !document.getElementById("auth-panel").hidden,
  listText: document.getElementById("alerts-list").textContent.replace(/\s+/g, " ").trim().slice(0, 60),
  panelSignout: !!document.getElementById("auth-panel-signout"),
}))));
await page.screenshot({ path: "board-alerts-panel.png", clip: { x: 900, y: 0, width: 540, height: 620 } });
console.log(errs.length ? "errors:\n  " + errs.join("\n  ") : "no console errors");
await page.unrouteAll({ behavior: "ignoreErrors" });
await browser.close();
