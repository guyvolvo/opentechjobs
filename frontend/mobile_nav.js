// One menu for every page, on a phone only.
//
// The top bar was four or five controls in a row: the site's name, the
// repository, the theme, the account, and on the board a status light
// and a ticker as well. At 390px that reads as a wall before a reader
// has seen a single word of the page.
//
// So on a phone the bar is three stepped rules at the left edge, and
// everything it held opens downward as one list.
//
// Nothing here owns any behaviour. The theme button, the sign-in
// dialog and the account state all live where they already lived; this
// builds a list that points at them, so there is one implementation of
// each and this cannot drift from it. The only thing it reads for
// itself is the address to show at the top of the list, and it reads
// that the same way every other surface does.
(function () {
  const BREAKPOINT = 640;
  const KEY = "iljobs_auth_tokens";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // The bar this page happens to have. The landing and contact pages
  // carry the display-face header; everything else carries the board's
  // dense topbar, whose controls sit in .topnav.
  const hero = document.querySelector("header.hero-bar-head");
  const topbar = document.querySelector(".topbar > .container");
  const host = hero || topbar;
  if (!host) return;

  const icons = {
    person: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><circle cx="8" cy="5" r="3" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M2.5 14c.6-3 2.7-4.5 5.5-4.5s4.9 1.5 5.5 4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    bookmark: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M4 2h8v12l-4-3-4 3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    bell: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M4 11V7a4 4 0 0 1 8 0v4l1 1.5H3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M6.5 14a1.5 1.5 0 0 0 3 0" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
    chat: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M2.5 3h11v8h-6l-3 2.5V11h-2z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    code: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M5.5 4L2 8l3.5 4M10.5 4L14 8l-3.5 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    board: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><rect x="2" y="3" width="12" height="10" rx="1" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M2 6.5h12" stroke="currentColor" stroke-width="1.5"/></svg>',
    github: '<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>',
    sun: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><circle cx="8" cy="8" r="3.2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3 3l1.1 1.1M11.9 11.9L13 13M13 3l-1.1 1.1M4.1 11.9L3 13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    moon: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M13.5 9.5A5.6 5.6 0 0 1 6.5 2.5a5.6 5.6 0 1 0 7 7z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    out: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M6 2.5H3v11h3M10 5l3 3-3 3M13 8H6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  function email() {
    try {
      const t = JSON.parse(localStorage.getItem(KEY) || "null");
      if (!t || !t.id_token) return "";
      const p = JSON.parse(atob(t.id_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      return p.email || p["cognito:username"] || "";
    } catch { return ""; }
  }

  // The page's own theme button, wherever it lives. Forwarding a click
  // to it keeps one implementation of the theme rather than a second
  // one here that has to remember the same storage key.
  const themeBtn = () => document.getElementById("hero-theme") || document.getElementById("theme-toggle");
  const isDark = () => document.documentElement.getAttribute("data-theme") === "dark";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "hero-nav-toggle";
  toggle.id = "mobile-nav-toggle";
  toggle.setAttribute("aria-label", "Menu");
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-controls", "mobile-nav-panel");
  toggle.innerHTML = '<span class="hero-nav-bars" aria-hidden="true"><i></i><i></i><i></i></span>';

  const panel = document.createElement("nav");
  panel.className = "hero-nav-panel";
  panel.id = "mobile-nav-panel";
  panel.setAttribute("aria-label", "Menu");
  panel.hidden = true;

  // First child, so it is the left edge of the bar and the first thing
  // a keyboard reaches.
  host.insertBefore(toggle, host.firstChild);
  // Into the same element the toggle went into, so the list lines up
  // under the icon: both bars already pad themselves to the page
  // margin, and a panel one level up would be indented past it.
  host.appendChild(panel);

  // The board itself, not merely a page that borrows the board's bar.
  // The first version asked whether this page had that bar, which is
  // true of the account, stats and privacy pages too, so the one link
  // most of them needed was the one they did not get.
  const onBoard = () => !!document.getElementById("jobs-body");

  function build() {
    const who = email();
    const rows = [];
    if (who) {
      rows.push(`<div class="hero-nav-who"><span class="hero-avatar" aria-hidden="true">${esc(who[0].toUpperCase())}</span><span class="hero-nav-email">${esc(who)}</span></div>`);
    } else {
      rows.push(`<button type="button" class="hero-nav-item hero-nav-signin" data-act="signin">${icons.person}Sign in</button>`);
    }
    if (!onBoard()) rows.push(`<a class="hero-nav-item" href="/board">${icons.board}Jobs</a>`);
    if (who) {
      rows.push(`<a class="hero-nav-item" href="/account">${icons.person}My profile</a>`);
      rows.push(`<a class="hero-nav-item" href="/board?starred=1">${icons.bookmark}Saved jobs</a>`);
      rows.push(`<a class="hero-nav-item" href="/account#alerts">${icons.bell}Alerts</a>`);
    }
    rows.push(`<a class="hero-nav-item" href="/api/help">${icons.code}API reference</a>`);
    rows.push(`<a class="hero-nav-item" href="/contact">${icons.chat}Contact</a>`);
    rows.push(`<a class="hero-nav-item" href="https://github.com/guyvolvo/opentechjobs" target="_blank" rel="noopener">${icons.github}GitHub</a>`);
    rows.push(`<button type="button" class="hero-nav-item" data-act="theme">${isDark() ? icons.sun : icons.moon}${isDark() ? "Light mode" : "Dark mode"}</button>`);
    if (who) rows.push(`<button type="button" class="hero-nav-item hero-nav-out" data-act="signout">${icons.out}Log out</button>`);
    panel.innerHTML = rows.join("");
  }

  function setOpen(on) {
    if (on) build();
    panel.hidden = !on;
    host.classList.toggle("nav-open", on);
    toggle.setAttribute("aria-expanded", String(on));
  }

  toggle.addEventListener("click", () => setOpen(panel.hidden));
  document.addEventListener("click", (e) => {
    if (!panel.hidden && !panel.contains(e.target) && !toggle.contains(e.target)) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    // The sign-in dialog opens from this menu and closes on Escape too.
    // Without this guard one press shut both, and dismissing the dialog
    // left the menu gone as well.
    if (e.key === "Escape" && !document.getElementById("signin-scrim")) setOpen(false);
  });

  panel.addEventListener("click", (e) => {
    const item = e.target.closest("[data-act]");
    if (!item) return;
    const act = item.getAttribute("data-act");
    if (act === "theme") {
      const b = themeBtn();
      if (b) b.click();
      build();       // the row now offers the other direction
      return;
    }
    if (act === "signout") {
      try { localStorage.removeItem(KEY); } catch {}
      setOpen(false);
      // Each page draws its own signed-out bar; reloading is the one
      // move that is correct on all of them.
      location.reload();
      return;
    }
    if (act === "signin") {
      setOpen(false);
      // openSignIn is the in-page dialog on the landing and contact
      // pages. The board and the rest have no such dialog, so they go
      // to the account page, which is where their sign-in has always
      // lived.
      if (typeof window.openSignIn === "function") window.openSignIn();
      else location.href = "/account";
    }
  });

  // Rebuilt after a sign-in, so the menu names the reader without a
  // reload. auth.js calls every render sink it has been given.
  if (typeof setAuthRenderSink === "function") {
    const prior = window.__navRenderSink;
    if (!prior) {
      window.__navRenderSink = true;
      document.addEventListener("iljobs:auth", () => { if (!panel.hidden) build(); });
    }
  }

  // Widened past the breakpoint with the menu open, it would stay in
  // the DOM doing nothing visible until the next phone-width visit.
  if (window.matchMedia) {
    const wide = window.matchMedia(`(min-width: ${BREAKPOINT + 1}px)`);
    const sync = () => { if (wide.matches) setOpen(false); };
    if (wide.addEventListener) wide.addEventListener("change", sync);
    else wide.addListener(sync);
  }
})();
