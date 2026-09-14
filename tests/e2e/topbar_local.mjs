// The phone topbar from the working tree, in WebKit (iPhone 14) and
// Chromium (Pixel 7), signed out and signed in. Measures the bar's height
// and every control's tap box, and screenshots the top of the page.
// Run from tests/e2e:  node topbar_local.mjs
import { chromium, devices, webkit } from "@playwright/test";
import { spawn } from "node:child_process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8822", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);

const stub = (signedIn) => `
  try { localStorage.setItem("iljobs_theme", "dark"); } catch {}
  ${signedIn ? `try {
    const payload = btoa(JSON.stringify({ email: "qa@example.com", exp: 4102444800 })).replace(/=+$/, "");
    localStorage.setItem("iljobs_auth_tokens", JSON.stringify({ id_token: "x." + payload + ".y", access_token: "a", refresh_token: "r", expires_at: 4102444800000 }));
  } catch {}` : ""}
  const ok = (b) => Promise.resolve(new Response(JSON.stringify(b), { status: 200, headers: { "Content-Type": "application/json" } }));
  const real = window.fetch.bind(window);
  window.fetch = (i, init) => {
    const u = String(i && i.url ? i.url : i);
    if (/\\.(woff2|css|js|svg|png)(\\?|$)/.test(u) && !u.includes("/api/")) return real(i, init);
    if (u.includes("/me/profile")) return ok({ profile: { skills: [] } });
    if (u.includes("/me/")) return ok({ alerts: [], saved: [] });
    if (u.includes("/jobs?")) return ok({ jobs: [], total: 0, limit: 50, offset: 0, matched_skills: [] });
    return ok({});
  };`;

for (const [name, engine, device] of [["iphone", webkit, devices["iPhone 14"]], ["android", chromium, devices["Pixel 7"]]]) {
  for (const signedIn of [false, true]) {
    const browser = await engine.launch();
    const context = await browser.newContext({ ...device, colorScheme: "dark" });
    await context.addInitScript(stub(signedIn));
    const page = await context.newPage();
    await page.goto("http://127.0.0.1:8822/index.html", { waitUntil: "networkidle" });
    await page.waitForTimeout(1500);
    const m = await page.evaluate(() => {
      const box = (el) => { const b = el.getBoundingClientRect(); return { x: Math.round(b.left), w: Math.round(b.width), h: Math.round(b.height) }; };
      const bar = document.querySelector(".topbar");
      const controls = [...document.querySelectorAll(".topnav > a, .topnav > button, .topnav .auth-area > button, .topnav .auth-area > a")]
        .filter((el) => el.offsetParent !== null)
        .map((el) => ({ label: el.textContent.trim().slice(0, 10) || el.getAttribute("aria-label") || el.className, ...box(el),
          fontSize: getComputedStyle(el).fontSize }));
      return { barHeight: Math.round(bar.getBoundingClientRect().height), viewport: innerWidth,
        overflow: document.documentElement.scrollWidth > innerWidth, controls };
    });
    const state = signedIn ? "signed-in" : "signed-out";
    console.log(name, state, JSON.stringify(m));
    await page.screenshot({ path: `topbar-local-${name}-${state}.png`, clip: { x: 0, y: 0, width: device.viewport.width, height: 150 } });
    await browser.close();
  }
}
server.kill();
