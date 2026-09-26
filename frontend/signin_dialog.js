// The sign-in dialog, for every page that has one.
//
// It used to live in hero_account.js, which only the landing and contact
// pages load, so the board grew its own panel hanging under the Sign in
// button instead: the same four controls, a different shape, in a
// different place. Two implementations of one screen, and they drifted.
//
// This is that dialog, loaded everywhere, opened by name. auth.js still
// does all the work; this is the window it needs and nothing more.
(function () {
// The sign-in itself, on this page rather than a trip to /account.
// auth.js does the work; this is the dialog it needs and nothing more.
function openSignIn() {
  if (document.getElementById("signin-scrim")) return;
  const wrap = document.createElement("div");
  wrap.id = "signin-scrim";
  wrap.className = "signin-scrim";
  wrap.innerHTML = `
    <div class="signin-dialog" role="dialog" aria-modal="true" aria-labelledby="signin-title">
      <div class="signin-dialog-head">
        <h2 class="account-block-title" id="signin-title">Log in</h2>
        <button type="button" class="signin-close" id="signin-close" aria-label="Close">&times;</button>
      </div>
      <button class="auth-provider-btn" id="signin-google" type="button">
        <svg viewBox="0 0 18 18" width="16" height="16" aria-hidden="true"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z"/><path fill="#FBBC05" d="M3.964 10.71c-.18-.54-.282-1.117-.282-1.71s.102-1.17.282-1.71V4.958H.957C.348 6.173 0 7.548 0 9s.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/></svg>
        <span>Continue with Google</span>
      </button>
      <button class="auth-provider-btn" id="signin-github" type="button">
        <svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
        <span>Continue with GitHub</span>
      </button>
      <div class="auth-divider">or</div>
      <form class="auth-email-form" id="signin-email-form">
        <input type="email" id="signin-email-input" placeholder="you@example.com" required autocomplete="email" />
        <button class="btn" type="submit">Send code</button>
      </form>
      <form class="auth-email-form" id="signin-otp-form" hidden>
        <input type="text" id="signin-otp-input" placeholder="Verification code" inputmode="numeric" pattern="[0-9]{4,10}" required autocomplete="one-time-code" />
        <button class="btn" type="submit">Verify</button>
      </form>
      <p class="auth-error" id="signin-auth-error" hidden></p>
    </div>`;
  document.body.appendChild(wrap);

  const err = document.getElementById("signin-auth-error");
  setAuthErrorSink((msg) => { err.textContent = msg; err.hidden = false; });
  const clear = () => { err.hidden = true; };
  const close = () => { wrap.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  wrap.addEventListener("click", (e) => { if (e.target === wrap) close(); });
  document.getElementById("signin-close").addEventListener("click", close);
  document.getElementById("signin-google").addEventListener("click", () => { clear(); startGoogleSignIn(); });
  document.getElementById("signin-github").addEventListener("click", () => { clear(); startGithubSignIn(); });
  document.getElementById("signin-email-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clear();
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      await startEmailSignIn(document.getElementById("signin-email-input").value.trim());
      document.getElementById("signin-email-form").hidden = true;
      document.getElementById("signin-otp-form").hidden = false;
      document.getElementById("signin-otp-input").focus();
    } catch (e2) {
      err.textContent = e2.message || "Could not send a code. Try again.";
      err.hidden = false;
    } finally {
      btn.disabled = false;
    }
  });
  document.getElementById("signin-otp-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clear();
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      await verifyEmailOtp(document.getElementById("signin-otp-input").value.trim());
      close();
      notifyAuthRender();  // the bar becomes My Account, on the page they were reading
    } catch (e2) {
      err.textContent = e2.message === "CodeMismatchException" ? "Wrong code, try again." : e2.message || "Could not verify that code.";
      err.hidden = false;
    } finally {
      btn.disabled = false;
    }
  });
  document.getElementById("signin-email-input").focus();
}

  // Opened from the bar on the landing page, the top bar on the board,
  // and the phone menu on both.
  window.openSignIn = openSignIn;
})();
