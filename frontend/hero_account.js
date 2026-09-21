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

  const icons = {
    person: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><circle cx="8" cy="5" r="3" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M2.5 14c.6-3 2.7-4.5 5.5-4.5s4.9 1.5 5.5 4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    bookmark: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M4 2h8v12l-4-3-4 3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    bell: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M4 11V7a4 4 0 0 1 8 0v4l1 1.5H3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M6.5 14a1.5 1.5 0 0 0 3 0" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
    chat: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M2.5 3h11v8h-6l-3 2.5V11h-2z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    out: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M6 2.5H3v11h3M10 5l3 3-3 3M13 8H6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  function render() {
    const t = tokens();
    const who = t && t.id_token ? email(t.id_token) : "";
    if (!who) {
      host.innerHTML = '<a class="hero-account-btn" href="/account">Sign in</a>';
      return;
    }
    const initial = esc(who[0].toUpperCase());
    host.innerHTML = `
      <button type="button" class="hero-account-btn" id="hero-account-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="hero-menu">
        <span class="hero-avatar" aria-hidden="true">${initial}</span>My Account
      </button>
      <div class="hero-menu" id="hero-menu" role="menu" hidden>
        <div class="hero-menu-head"><span class="hero-avatar" aria-hidden="true">${initial}</span><span class="hero-menu-email" title="${esc(who)}">${esc(who)}</span></div>
        <a role="menuitem" href="/account">${icons.person}My Profile</a>
        <a role="menuitem" href="/board?starred=1">${icons.bookmark}Saved Jobs</a>
        <a role="menuitem" href="/account#alerts">${icons.bell}Alerts</a>
        <a role="menuitem" href="/contact">${icons.chat}Contact Support</a>
        <button type="button" role="menuitem" class="hero-menu-out" id="hero-logout">${icons.out}Log Out</button>
      </div>`;
    const btn = document.getElementById("hero-account-btn");
    const menu = document.getElementById("hero-menu");
    const open = (on) => { menu.hidden = !on; btn.setAttribute("aria-expanded", String(on)); };
    btn.addEventListener("click", () => open(menu.hidden));
    document.addEventListener("click", (e) => { if (!host.contains(e.target)) open(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") open(false); });
    document.getElementById("hero-logout").addEventListener("click", () => {
      try { localStorage.removeItem(KEY); } catch {}
      render();
    });
  }
  render();
})();
