// The account corner of the landing and contact pages. Signed out it is
// one word, "Sign in", to the account page. Signed in it is "My Account"
// with the reader's initial, opening a short menu: the account page, the
// saved listings, alerts, the contact page, and log out. The tokens are
// the same localStorage entry the board and account page keep
// (iljobs_auth_tokens), read only for the address to show; nothing here
// verifies them, the API does that on every request that needs them.
(function () {
  const KEY = "iljobs_auth_tokens";
  const host = document.getElementById("hero-account");
  if (!host) return;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function tokens() {
    try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch { return null; }
  }
  function email(idToken) {
    try {
      const payload = JSON.parse(atob(idToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      return payload.email || payload["cognito:username"] || "";
    } catch { return ""; }
  }

  const isDark = () => document.documentElement.getAttribute("data-theme") === "dark";
  function toggleTheme() {
    if (isDark()) document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", "dark");
    try { localStorage.setItem("iljobs_theme", isDark() ? "dark" : "light"); } catch {}
  }

  const icons = {
    person: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><circle cx="8" cy="5" r="3" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M2.5 14c.6-3 2.7-4.5 5.5-4.5s4.9 1.5 5.5 4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    bookmark: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M4 2h8v12l-4-3-4 3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    chart: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M2 13.5h12M4 13V8M7.3 13V4.5M10.6 13V9.5M13.9 13V6.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    bell: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M4 11V7a4 4 0 0 1 8 0v4l1 1.5H3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M6.5 14a1.5 1.5 0 0 0 3 0" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
    chat: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M2.5 3h11v8h-6l-3 2.5V11h-2z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    sun: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><circle cx="8" cy="8" r="3.2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3 3l1.1 1.1M11.9 11.9L13 13M13 3l-1.1 1.1M4.1 11.9L3 13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    moon: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M13.5 9.5A5.6 5.6 0 0 1 6.5 2.5a5.6 5.6 0 1 0 7 7z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    out: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M6 2.5H3v11h3M10 5l3 3-3 3M13 8H6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  function render() {
    const t = tokens();
    const who = t && t.id_token ? email(t.id_token) : "";
    // The bar's own theme button only exists for the signed-out state,
    // where there is no menu to carry the row.
    const barTheme = document.getElementById("hero-theme");
    if (barTheme) barTheme.hidden = !!who;
    if (!who) {
      host.innerHTML = '<button type="button" class="hero-account-btn" id="hero-signin">Sign in</button>';
      // The dialog lives in signin_dialog.js, which every page loads,
      // so the board opens the same one rather than its own panel.
      document.getElementById("hero-signin").addEventListener("click", () => window.openSignIn());
      return;
    }
    // Their Google photo when they signed in that way, the first letter
    // of the address otherwise. avatarHtml in auth.js decides which.
    const avatar = avatarHtml(who, t.id_token);
    host.innerHTML = `
      <button type="button" class="hero-account-btn" id="hero-account-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="hero-menu">
        ${avatar}My Account
      </button>
      <div class="hero-menu" id="hero-menu" role="menu" hidden>
        <div class="hero-menu-head">${avatar}<span class="hero-menu-email" title="${esc(who)}">${esc(who)}</span></div>
        <a role="menuitem" href="/account">${icons.person}My Profile</a>
        <a role="menuitem" href="/board?starred=1">${icons.bookmark}Saved Jobs</a>
        <a role="menuitem" href="/account#alerts">${icons.bell}Alerts</a>
        <a role="menuitem" href="/stats">${icons.chart}Statistics</a>
        <a role="menuitem" href="/contact">${icons.chat}Contact Support</a>
        <button type="button" role="menuitem" id="hero-menu-theme">${isDark() ? icons.sun : icons.moon}<span>${isDark() ? "Light mode" : "Dark mode"}</span></button>
        <button type="button" role="menuitem" class="hero-menu-out" id="hero-logout">${icons.out}Log Out</button>
      </div>`;
    const btn = document.getElementById("hero-account-btn");
    const menu = document.getElementById("hero-menu");
    const open = (on) => { menu.hidden = !on; btn.setAttribute("aria-expanded", String(on)); };
    btn.addEventListener("click", () => open(menu.hidden));
    document.addEventListener("click", (e) => { if (!host.contains(e.target)) open(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") open(false); });
    // The theme row. Repaints itself rather than re-rendering the whole
    // menu, which would shut it on the click that opened the change.
    const themeRow = document.getElementById("hero-menu-theme");
    themeRow.addEventListener("click", () => {
      toggleTheme();
      themeRow.innerHTML = `${isDark() ? icons.sun : icons.moon}<span>${isDark() ? "Light mode" : "Dark mode"}</span>`;
      paintThemeBtn();
    });
    document.getElementById("hero-logout").addEventListener("click", () => {
      try { localStorage.removeItem(KEY); } catch {}
      render();
    });
  }

  // The theme. Written to the same key the board's own toggle writes,
  // so the choice follows a reader between pages.
  //
  // Signed in, it is a row in the account menu. Signed out there is no
  // menu to put it in, so the bar keeps its own button; hiding it in
  // that state would leave a reader no way to change the theme at all.
  const themeBtn = document.getElementById("hero-theme");
  const paintThemeBtn = () => { if (themeBtn) themeBtn.textContent = isDark() ? "Light" : "Dark"; };
  if (themeBtn) {
    themeBtn.addEventListener("click", () => { toggleTheme(); paintThemeBtn(); });
    paintThemeBtn();
  }

  setAuthRenderSink(render);
  render();
})();
