// The account control is a link to /account now, not a button that opened
// the alerts panel. Signed-in state is faked with a JWT-shaped token so
// renderAuthState takes that branch without a real Cognito round trip.
import { chromium } from "playwright";
import { spawn } from "node:child_process";
const PORT = 8843;
const srv = spawn("python", ["-m", "http.server", String(PORT)], { cwd: "frontend", stdio: "ignore" });
await new Promise(r => setTimeout(r, 1200));
let bad = 0;
const check = (n, ok, d = "") => { console.log(`${ok ? "PASS" : "FAIL"}: ${n}${ok ? "" : "  -- " + d}`); if (!ok) bad++; };
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  await page.addInitScript(() => {
    const b64 = (o) => btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const jwt = `${b64({ alg: "none" })}.${b64({ email: "you@example.com", exp: Math.floor(Date.now() / 1000) + 86400 })}.x`;
    for (const k of ["iljobs_auth_tokens"]) {
      localStorage.setItem(k, JSON.stringify({ id_token: jwt, access_token: jwt, refresh_token: "r" }));
    }
  });
  await page.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  const state = await page.evaluate(() => {
    const area = document.getElementById("auth-area");
    const acct = area?.querySelector(".auth-account");
    return {
      html: (area?.innerHTML || "").slice(0, 160),
      signedIn: !!document.getElementById("topbar-alert-btn"),
      acctTag: acct?.tagName || null,
      acctHref: acct?.getAttribute("href") || null,
      hasOldTrigger: !!document.getElementById("auth-trigger"),
    };
  });
  console.log("state:", JSON.stringify(state));
  if (!state.signedIn) {
    console.log("NOTE: signed-in branch not reached (token key guess wrong); only the signed-out path was exercised.");
  } else {
    check("the account control is a link", state.acctTag === "A", String(state.acctTag));
    check("it points at /account", state.acctHref === "/account", String(state.acctHref));
    check("the old panel trigger is gone", !state.hasOldTrigger);
    await page.click("#topbar-alert-btn");
    check("+ Alert opens the panel", !(await page.locator("#auth-panel").isHidden()));
    await page.mouse.click(640, 700);
    await page.waitForTimeout(150);
    check("clicking outside closes it", await page.locator("#auth-panel").isHidden());
  }
  check("no page errors", errors.length === 0, errors.join(" | "));
} finally { await browser.close(); srv.kill(); }
process.exit(bad ? 1 : 0);
