// /hero: desktop and phone, light and dark. Checks the numbers ticker
// carries real numbers, the logo row under it is half its height, drops a
// logo that fails to load, and runs the other way, the green band shows
// the same margin above and below, the wordmark fits, nothing scrolls
// sideways, the feature rows reveal on scroll, and a row thrown with the
// mouse flies the way it was thrown, then eases back into its drift.
// Screenshots.
// Run from tests/e2e:  node hero_check.mjs
import { chromium, devices } from "@playwright/test";
import { spawn } from "node:child_process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ROOT = "C:/Users/admin/Desktop/Test scripts";
const server = spawn(`${ROOT}/.venv/Scripts/python.exe`, ["-m", "http.server", "8832", "--bind", "127.0.0.1"],
  { cwd: `${ROOT}/frontend`, stdio: "ignore" });
await sleep(1500);

const STATS = {
  totals: { open_jobs: 176465, companies_hiring: 4134 },
  throughput: { new_jobs_24h: 38776, closed_jobs_24h: 6727 },
  age: { median_open_days: 21.3 },
  location: { israel: 2984 },
  workplace: [{ workplace: "unstated", n: 96499 }, { workplace: "remote", n: 25841 }, { workplace: "onsite", n: 34716 }],
  top_companies_logos: [
    ...["#ff9900", "#232f3e", "#4285f4", "#00a4ef", "#111111", "#e4002b"].map((fill, i) => ({
      domain: `c${i}.com`, name: `Company ${i}`, n: 1000 - i,
      logo_url: "data:image/svg+xml," + encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'><circle cx='5' cy='5' r='5' fill='${fill}'/></svg>`),
    })),
    { domain: "broken.com", name: "Broken", n: 10, logo_url: "http://127.0.0.1:8832/no-such-logo.png" },
  ],
};

const failures = [];
const check = (name, ok, detail = "") => { console.log(`${ok ? "PASS" : "FAIL"}: ${name}${ok ? "" : "  -- " + detail}`); if (!ok) failures.push(name); };

const browser = await chromium.launch();
for (const [label, device] of [["desktop", { viewport: { width: 1440, height: 900 } }], ["phone", devices["Pixel 7"]]]) {
  for (const theme of ["light", "dark"]) {
    const context = await browser.newContext({ ...device });
    await context.addInitScript(({ theme, stats }) => {
      try { localStorage.setItem("iljobs_theme", theme); } catch {}
      const real = window.fetch.bind(window);
      window.fetch = (i, init) => String(i && i.url ? i.url : i).includes("stats.json")
        ? Promise.resolve(new Response(JSON.stringify(stats), { status: 200, headers: { "Content-Type": "application/json" } }))
        : real(i, init);
    }, { theme, stats: STATS });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto("http://127.0.0.1:8832/hero.html", { waitUntil: "networkidle" });
    await page.waitForTimeout(700);
    const tag = `${label}/${theme}`;

    const m = await page.evaluate(() => {
      const x = (id) => new DOMMatrixReadOnly(getComputedStyle(document.getElementById(id)).transform).m41;
      const word = document.querySelector(".hero-word").getBoundingClientRect();
      const bandBox = document.querySelector(".hero-block").getBoundingClientRect();
      return { overflow: document.documentElement.scrollWidth > innerWidth, wordRight: word.right, vw: innerWidth,
        bandLeft: bandBox.left, bandRight: bandBox.right, clientW: document.documentElement.clientWidth,
        wordHref: document.querySelector(".hero-word")?.getAttribute("href"),
        tickerPx: parseFloat(getComputedStyle(document.getElementById("ticker-top")).fontSize),
        top0: x("ticker-top"), bottom0: x("ticker-logos"),
        topText: document.getElementById("ticker-top").textContent,
        tiles: document.querySelectorAll("#ticker-logos .hero-logo").length,
        broken: document.querySelectorAll('#ticker-logos img[src*="no-such-logo"]').length,
        numbersH: document.querySelector(".hero-ticker.to-right").getBoundingClientRect().height,
        logosH: document.querySelector(".hero-logos").getBoundingClientRect().height,
        tileH: document.querySelector("#ticker-logos .hero-logo")?.getBoundingClientRect().height,
        cta: document.getElementById("cta-count").textContent, theme: document.documentElement.getAttribute("data-theme") };
    });
    // The rows sit on the page's own paper now, fade out at both window
    // edges, and the marks are grey rather than a colour wall.
    const band = await page.evaluate(() => {
      const row = document.querySelector(".hero-logos");
      const img = document.querySelector("#ticker-logos .hero-logo img");
      const block = document.querySelector(".hero-block");
      const cs = getComputedStyle(row);
      return {
        mask: (cs.maskImage || cs.webkitMaskImage || "").includes("gradient"),
        grey: getComputedStyle(img).filter.includes("grayscale"),
        blockBg: getComputedStyle(block).backgroundColor,
        numColour: getComputedStyle(document.querySelector("#ticker-top .hero-num")).color,
      };
    });
    check(`${tag}: the rows fade at the window edges`, band.mask, JSON.stringify(band));
    check(`${tag}: the logos are grey, not a colour wall`, band.grey, JSON.stringify(band));
    check(`${tag}: the rows sit on the page, no band of their own`,
      band.blockBg === "rgba(0, 0, 0, 0)", band.blockBg);
    // A salary estimate that reads as a posted figure is the wrong kind of
    // wrong, so the qualification is checked, not assumed.
    const proof = await page.evaluate(() =>
      [...document.querySelectorAll(".hero-proof li")].map((li) => li.textContent.replace(/\s+/g, " ").trim()));
    check(`${tag}: the promise is two lines, CV first`,
      proof.length === 2 && /^Have your CV analyzed for keywords/.test(proof[0]), JSON.stringify(proof).slice(0, 160));
    check(`${tag}: the salary line says it may be wrong`,
      /estimation algorithm/.test(proof[1] || "") && /may be inaccurate/.test(proof[1] || ""),
      (proof[1] || "").slice(0, 120));
    await page.screenshot({ path: `hero-${label}-${theme}-top.png` });
    await page.waitForTimeout(1500);
    const later = await page.evaluate(() => {
      const x = (id) => new DOMMatrixReadOnly(getComputedStyle(document.getElementById(id)).transform).m41;
      return { top1: x("ticker-top"), bottom1: x("ticker-logos") };
    });
    check(`${tag}: no sideways scroll`, !m.overflow);
    check(`${tag}: the wordmark links to the board`, m.wordHref === "/", String(m.wordHref));
    check(`${tag}: the green band runs edge to edge`, m.bandLeft === 0 && Math.abs(m.bandRight - m.clientW) < 1, JSON.stringify({ l: m.bandLeft, r: m.bandRight, w: m.clientW }));
    check(`${tag}: the wordmark fits the page`, m.wordRight <= m.vw, `${m.wordRight} > ${m.vw}`);
    check(`${tag}: the numbers ticker carries live numbers`, m.topText.includes("176,465 open jobs") && m.topText.includes("25,841 remote") && !/Israel|median|24 hours/.test(m.topText), m.topText.slice(0, 120));
    check(`${tag}: the logo row shows the logos and drops the broken one`, m.tiles >= 60 && m.tiles % 6 === 0 && m.broken === 0, JSON.stringify({ tiles: m.tiles, broken: m.broken }));
    // The logos no longer track the ticker's type size: at proof-strip
    // scale half a line is too small to recognise a mark. They keep their
    // own size, square, and stay in the band rather than dwarfing it.
    check(`${tag}: the logos are recognisable without dwarfing the numbers`,
      m.logosH >= 28 && m.logosH <= 50 && Math.abs(m.tileH - m.logosH) < 1 && m.logosH <= m.numbersH * 1.4,
      JSON.stringify({ numbersH: m.numbersH, logosH: m.logosH, tileH: m.tileH }));
    check(`${tag}: numbers move right, logos move left`, later.top1 > m.top0 && later.bottom1 < m.bottom0, JSON.stringify({ ...m, ...later }));
    // Throw the logo row to the right, against its leftward drift.
    if (label === "desktop") {
      const row = await page.locator(".hero-logos").boundingBox();
      const y = row.y + row.height / 2;
      const speed = async () => {
        const read = () => page.evaluate(() => {
          const tr = document.getElementById("ticker-logos");
          return { x: new DOMMatrixReadOnly(getComputedStyle(tr).transform).m41, half: tr.scrollWidth / 2, t: performance.now() };
        });
        const a = await read();
        await page.waitForTimeout(150);
        const b = await read();
        let d = b.x - a.x;
        if (d > a.half / 2) d -= a.half;
        if (d < -a.half / 2) d += a.half;
        return Math.round(d / ((b.t - a.t) / 1000));
      };
      const drift = await speed();
      await page.mouse.move(300, y);
      await page.mouse.down();
      for (let i = 1; i <= 10; i++) { await page.mouse.move(300 + i * 50, y); await page.waitForTimeout(6); }
      await page.mouse.up();
      const thrown = await speed();
      await page.waitForTimeout(3000);
      const settled = await speed();
      const speeds = JSON.stringify({ drift, thrown, settled });
      check(`${tag}: the logo row drifts left before the throw`, drift < 0, speeds);
      check(`${tag}: a throw to the right sends the logos right, fast`, thrown > Math.abs(drift) * 4, speeds);
      check(`${tag}: after the spin the logos ease back into their drift`, settled < 0 && Math.abs(settled - drift) <= Math.abs(drift) * 0.3, speeds);
    }
    // The product shot: one image of a device with the board inside it,
    // the laptop on a desktop and the phone on a phone, sitting in the card.
    const product = await page.evaluate(async () => {
      const dev = document.querySelector(".showcase-device");
      const cs = getComputedStyle(dev);
      const url = cs.backgroundImage.match(/url\("?([^")]+)"?\)/)?.[1] || "";
      let ok = false, ratio = 0;
      try {
        const img = new Image();
        img.src = url;
        await img.decode();
        ok = img.naturalWidth > 300;
        ratio = img.naturalWidth / img.naturalHeight;
      } catch {}
      const sect = document.querySelector(".hero-showcase").getBoundingClientRect();
      const box = dev.getBoundingClientRect();
      const primary = document.querySelector(".hero-primary");
      const secondary = document.querySelector(".hero-secondary");
      const lede = document.querySelector(".hero-lede");
      return {
        url, ok, ratio,
        boxRatio: box.width / box.height,
        inside: box.left >= sect.left - 1 && box.right <= sect.right + 1,
        cut: box.bottom >= sect.bottom - 2,
        cta: primary.textContent.replace(/\s+/g, " ").trim(),
        href: primary.getAttribute("href"),
        apiHref: secondary.getAttribute("href"),
        claim: document.querySelector(".hero-claim").textContent.trim(),
        lede: lede.textContent.replace(/\s+/g, " ").trim(),
        ledePx: parseFloat(getComputedStyle(lede).fontSize),
        ledeWidth: lede.getBoundingClientRect().width,
        ledeLines: lede.getClientRects().length,
        wordPx: parseFloat(getComputedStyle(document.querySelector(".hero-word")).fontSize),
      };
    });
    check(`${tag}: the device image loads, and it is the right one`,
      product.ok && product.url.includes(label === "phone" ? "device-iphone" : "device-macbook"),
      JSON.stringify({ url: product.url.slice(-28), ok: product.ok }));
    check(`${tag}: the device is cut off at the foot, not shrunk to fit`,
      product.boxRatio > product.ratio + 0.05,
      JSON.stringify({ box: product.boxRatio.toFixed(3), image: product.ratio.toFixed(3) }));
    check(`${tag}: the device sits inside the card`, product.inside, String(product.inside));
    check(`${tag}: the claim leads, the brand does not`,
      product.claim === "Straight from the source." && product.wordPx <= 82,
      JSON.stringify({ claim: product.claim, wordPx: product.wordPx }));
    check(`${tag}: the lede carries the live count and reads at size`,
      product.lede === "176,465 open jobs, read directly from company hiring systems and career sites."
      && product.ledePx >= 16 && product.ledeWidth <= 780,
      JSON.stringify({ lede: product.lede, px: product.ledePx, w: Math.round(product.ledeWidth) }));
    if (label === "desktop") {
      check(`${tag}: the lede is one line`, product.ledeLines === 1, `${product.ledeLines} lines`);
    }
    check(`${tag}: both doors are open, the board and the API`,
      /^Search open jobs/.test(product.cta) && product.href === "/" && product.apiHref === "/api/help",
      JSON.stringify({ cta: product.cta, href: product.href, api: product.apiHref }));
    check(`${tag}: the search link names the count`, m.cta === "176,465 open jobs", m.cta);
    const api = await page.evaluate(() => {
      const el = document.querySelector(".hero-api");
      return {
        has: !!el,
        link: el?.querySelector(".hero-api-link")?.getAttribute("href"),
        text: (el?.textContent || "").replace(/\s+/g, " ").trim(),
        showcase: (() => {
          const el = document.querySelector(".hero-showcase");
          const r = el.getBoundingClientRect();
          const cs = getComputedStyle(el);
          return { left: Math.round(r.left), right: Math.round(r.right),
            radius: parseFloat(cs.borderTopLeftRadius), vw: document.documentElement.clientWidth,
            captions: document.querySelectorAll(".band-note, .showcase-note").length };
        })(),
      };
    });
    check(`${tag}: the API has its own block and states its limits`,
      api.has && api.link === "/api/help" && /No key, no sign-up/.test(api.text) && /20 requests a second/.test(api.text),
      api.text.slice(0, 140));
    // The preview is a card now: held off both edges and rounded, with no
    // caption above it.
    check(`${tag}: the preview is inset from the window and rounded`,
      api.showcase.left >= 16 && api.showcase.vw - api.showcase.right >= 16
      && Math.abs(api.showcase.left - (api.showcase.vw - api.showcase.right)) <= 1
      && api.showcase.radius >= 16,
      JSON.stringify(api.showcase));
    check(`${tag}: the captions are gone`, api.showcase.captions === 0, String(api.showcase.captions));
    if (theme === "dark") check(`${tag}: dark theme applied`, m.theme === "dark", String(m.theme));

    const feature = page.locator(".hero-feature").last();
    const before = await feature.evaluate((el) => getComputedStyle(el.querySelector("h2")).opacity);
    await feature.scrollIntoViewIfNeeded();
    await page.waitForTimeout(1100);
    const after = await feature.evaluate((el) => ({ in: el.classList.contains("in"), opacity: getComputedStyle(el.querySelector("h2")).opacity }));
    check(`${tag}: a feature row reveals when scrolled to`, before === "0" && after.in && after.opacity === "1", JSON.stringify({ before, after }));
    await page.evaluate(() => window.scrollTo(0, document.querySelector(".hero-features").offsetTop - 20));
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `hero-${label}-${theme}-features.png` });
    check(`${tag}: no page errors`, errors.length === 0, errors.join(" | "));
    await context.close();
  }
}
await browser.close();
server.kill();
console.log(failures.length ? `\n${failures.length} failed` : "\nall passed");
process.exit(failures.length ? 1 : 0);
