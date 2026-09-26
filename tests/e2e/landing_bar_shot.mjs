// The landing page's top bar, working copy over the live site, desktop
// light and dark and phone. node tests/e2e/landing_bar_shot.mjs [tag]
import { chromium } from "playwright";
const tag = process.argv[2] || "now";
const browser = await chromium.launch();
const errs = [];
for (const [name, width, theme] of [["desk", 1440, "light"], ["desk-dark", 1440, "dark"], ["phone", 390, "light"]]) {
  const page = await browser.newPage({ viewport: { width, height: width < 800 ? 844 : 900 } });
  page.on("pageerror", (e) => errs.push(`${name}: ${e.message}`));
  await page.addInitScript((t) => { localStorage.setItem("iljobs_theme", t); }, theme);
  const LOCAL = ["hero_account", "hero_nav", "mobile_nav", "auth", "signin_dialog"];
  await page.route((u) => LOCAL.some((n) => u.pathname === `/${n}.js`), (r) =>
    r.fulfill({ path: `../../frontend${new URL(r.request().url()).pathname}`, contentType: "application/javascript" }));
  await page.route(/\/style\.css(\?|$)/, (r) => r.fulfill({ path: "../../frontend/style.css", contentType: "text/css" }));
  await page.route((u) => u.pathname === "/" || u.pathname === "/index.html", (r) => r.fulfill({ path: "../../frontend/index.html", contentType: "text/html" }));
  await page.goto("https://oceanofjobs.com/", { waitUntil: "networkidle" });
  await page.waitForTimeout(600);
  await page.screenshot({ path: `landing-bar-${tag}-${name}.png`, clip: { x: 0, y: 0, width, height: width < 800 ? 300 : 260 } });
  if (name === "desk") {
    const menu = await page.$(".site-menu[data-menu=resources] .site-menu-btn");
    if (menu) { await menu.click(); await page.waitForTimeout(300); await page.screenshot({ path: `landing-bar-${tag}-open.png`, clip: { x: 0, y: 0, width, height: 420 } }); }
  }
  await page.close();
}
console.log(errs.length ? errs.join("\n") : "no page errors");
await browser.close();
