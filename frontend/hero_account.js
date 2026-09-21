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
      host.innerHTML = '<button type="button" class="hero-account-btn" id="hero-signin">Sign in</button>';
      document.getElementById("hero-signin").addEventListener("click", openSignIn);
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
  // The sign-in itself, on this page rather than a trip to /account.
  // auth.js does the work; this is the dialog it needs and nothing more.
  function openSignIn() {
    if (document.getElementById("signin-scrim")) return;
    const wrap = document.createElement("div");
    wrap.id = "signin-scrim";
    wrap.className = "signin-scrim";
    wrap.innerHTML = `
      <div class="signin-dialog" role="dialog" aria-modal="true" aria-labelledby="hero-signin-title">
        <div class="signin-dialog-head">
          <h2 class="account-block-title" id="hero-signin-title">Sign in</h2>
          <button type="button" class="signin-close" id="signin-close" aria-label="Close">&times;</button>
        </div>
        <button class="auth-provider-btn" id="hero-google" type="button">
          <svg viewBox="0 0 18 18" width="16" height="16" aria-hidden="true"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z"/><path fill="#FBBC05" d="M3.964 10.71c-.18-.54-.282-1.117-.282-1.71s.102-1.17.282-1.71V4.958H.957C.348 6.173 0 7.548 0 9s.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/></svg>
          <span>Continue with Google</span>
        </button>
        <button class="auth-provider-btn" id="hero-github" type="button">
          <svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
          <span>Continue with GitHub</span>
        </button>
        <div class="auth-divider">or</div>
        <form class="auth-email-form" id="hero-email-form">
          <input type="email" id="hero-email-input" placeholder="you@example.com" required autocomplete="email" />
          <button class="btn" type="submit">Send code</button>
        </form>
        <form class="auth-email-form" id="hero-otp-form" hidden>
          <input type="text" id="hero-otp-input" placeholder="Verification code" inputmode="numeric" pattern="[0-9]{4,10}" required autocomplete="one-time-code" />
          <button class="btn" type="submit">Verify</button>
        </form>
        <p class="auth-error" id="hero-auth-error" hidden></p>
      </div>`;
    document.body.appendChild(wrap);

    const err = document.getElementById("hero-auth-error");
    setAuthErrorSink((msg) => { err.textContent = msg; err.hidden = false; });
    const clear = () => { err.hidden = true; };
    const close = () => { wrap.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    wrap.addEventListener("click", (e) => { if (e.target === wrap) close(); });
    document.getElementById("signin-close").addEventListener("click", close);
    document.getElementById("hero-google").addEventListener("click", () => { clear(); startGoogleSignIn(); });
    document.getElementById("hero-github").addEventListener("click", () => { clear(); startGithubSignIn(); });
    document.getElementById("hero-email-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      clear();
      const btn = e.target.querySelector("button");
      btn.disabled = true;
      try {
        await startEmailSignIn(document.getElementById("hero-email-input").value.trim());
        document.getElementById("hero-email-form").hidden = true;
        document.getElementById("hero-otp-form").hidden = false;
        document.getElementById("hero-otp-input").focus();
      } catch (e2) {
        err.textContent = e2.message || "Could not send a code. Try again.";
        err.hidden = false;
      } finally {
        btn.disabled = false;
      }
    });
    document.getElementById("hero-otp-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      clear();
      const btn = e.target.querySelector("button");
      btn.disabled = true;
      try {
        await verifyEmailOtp(document.getElementById("hero-otp-input").value.trim());
        close();
        render();  // the bar becomes My Account, on the page they were reading
      } catch (e2) {
        err.textContent = e2.message === "CodeMismatchException" ? "Wrong code, try again." : e2.message || "Could not verify that code.";
        err.hidden = false;
      } finally {
        btn.disabled = false;
      }
    });
    document.getElementById("hero-email-input").focus();
  }

  setAuthRenderSink(render);
  render();
})();
