// Sign-in page (spec #271). A same-origin classic script: the CSP forbids
// inline script. It asks the review server where the host authority is
// (GET /api/auth/status, open without a session), then talks to the authority
// directly, with the session cookie, on the hostname this page was loaded
// from. The review server never sees the password, the code or the secret.
//
// Nothing here is stored: no localStorage, no URL parameter, no log line. The
// authenticator secret arrives once, during first sign-in, and lives only in
// this page's memory until the step is left.

const SIGNIN_FAILED = "Sign-in failed. Check the username, password and code, "
  + "and that this device's clock is right.";
const SIGNIN_UNLOCK_CLI = "./sc auth unlock";
const SIGNIN_SETUP_CLI = "./sc auth user add <username>";
const SIGNIN_START_CLI = "./sc launch";
const SIGNIN_PASSWORD_MIN = 8;

// `next` is honoured only as a path on this origin. Anything else (another
// host, a scheme, a protocol-relative or backslash form, a control character,
// this page itself) falls back to "/".
function safeNext(raw, origin) {
  if (typeof raw !== "string" || raw[0] !== "/" || raw[1] === "/") return "/";
  if (/[\\\u0000-\u001f\u007f]/.test(raw)) return "/";
  let url;
  try { url = new URL(raw, origin); } catch { return "/"; }
  if (url.origin !== origin || url.pathname === "/signin.html") return "/";
  return url.pathname + url.search + url.hash;
}
// A redirect from the review server cannot carry the fragment (it never
// reaches a server), but the browser keeps it on this page's URL, so a
// `next` without one takes this page's.
function nextTarget(location) {
  const raw = new URLSearchParams(location.search).get("next");
  const next = safeNext(raw === null ? "/" : raw, location.origin);
  return next.includes("#") || !location.hash ? next : next + location.hash;
}
// The authority is reached on this page's hostname: the session cookie is
// bound to that exact host, and cookies ignore ports.
function signInAuthorityBase(url, location) {
  if (!url) return null;
  try {
    const target = new URL(url);
    target.hostname = location.hostname;
    return target.origin;
  } catch { return null; }
}
const groupKey = (secret) => String(secret || "").replace(/(.{4})(?=.)/g, "$1 ");

// Black on white with a four-module quiet zone, whatever the theme: a scanner
// needs the contrast. Returns the module count, or 0 when it cannot be drawn.
function drawSignInQr(canvas, text, encoder) {
  try {
    const qr = encoder(0, "M");
    qr.addData(text);
    qr.make();
    const count = qr.getModuleCount(), quiet = 4, scale = 4;
    const size = (count + 2 * quiet) * scale;
    canvas.width = size;
    canvas.height = size;
    const context = canvas.getContext("2d");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, size, size);
    context.fillStyle = "#000000";
    for (let row = 0; row < count; row += 1)
      for (let column = 0; column < count; column += 1)
        if (qr.isDark(row, column))
          context.fillRect((column + quiet) * scale, (row + quiet) * scale, scale, scale);
    return count;
  } catch { return 0; }
}

function createSignIn(env) {
  const { root, location } = env;
  const doc = env.document;
  const el = (tag, props = {}, ...kids) => {
    const node = doc.createElement(tag);
    Object.assign(node, props);
    for (const kid of kids) node.append(kid);
    return node;
  };
  const next = nextTarget(location);
  let base = null;          // the authority, once known
  let limitedUntil = 0;     // ms timestamp; attempts are refused until then
  let unlockCli = SIGNIN_UNLOCK_CLI;
  let ticker = null;        // the shown view's countdown timer
  let countdown = null;     // a form's countdown, waiting for its view to be shown
  let enrolment = null;     // first sign-in only: { enrolment, secret, otpauth_uri, ... }

  const call = async (origin, path, method = "GET", body = {}) => {
    const init = { method, credentials: "include" };
    if (method === "POST") {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    const response = await env.fetch(origin + path, init);
    const data = await response.json().catch(() => ({}));
    return { status: response.status, ok: response.ok, data,
      retryAfter: Number(response.headers?.get?.("Retry-After")) || 0 };
  };
  const errorCode = (answer) => answer.data?.error?.code || answer.data?.error || "";

  // ── view plumbing ──
  // One view at a time: showing a view stops the previous view's countdown
  // and starts this one's, if it has a form.
  const show = (heading, ...nodes) => {
    if (ticker) env.clearInterval(ticker);
    ticker = countdown ? env.setInterval(countdown, 1000) : null;
    countdown = null;
    const title = el("h2", { tabIndex: -1 }, heading);
    root.replaceChildren(title, ...nodes);
    return title;
  };
  const text = (line) => el("p", { className: "signin-text" }, line);
  const cli = (command) => el("code", { className: "signin-cli" }, command);
  const button = (label, primary = false, type = "button") =>
    el("button", { className: "act" + (primary ? " primary" : ""), type, textContent: label });
  const field = (form, id, label, props) => {
    const input = el("input", { id, name: id, ...props });
    form.append(el("label", { htmlFor: id }, label), input);
    return input;
  };
  // A form with one status line and one submit button. While attempts are
  // rate limited the button is disabled and the line counts down.
  const form = (submitLabel, onSubmit) => {
    const node = el("form", { className: "signin-form", noValidate: true });
    const notice = el("p", { className: "signin-notice", role: "alert" });
    const submit = button(submitLabel, true, "submit");
    const actions = el("div", { className: "signin-actions" }, submit);
    let busy = false;
    const paintLimit = () => {
      const left = Math.ceil((limitedUntil - env.now()) / 1000);
      if (left > 0) {
        notice.className = "signin-notice wait";
        notice.textContent = `Too many failed attempts. Try again in ${left} s, `
          + `or run ${unlockCli} on the host.`;
      } else if (notice.className === "signin-notice wait") {
        notice.className = "signin-notice";
        notice.textContent = "";
      }
      submit.disabled = busy || left > 0;
    };
    node.onsubmit = async (event) => {
      event.preventDefault();
      if (busy || limitedUntil > env.now()) return;
      busy = true;
      notice.className = "signin-notice";
      notice.textContent = "";
      paintLimit();
      try { await onSubmit((message) => { notice.textContent = message; }); }
      finally { busy = false; paintLimit(); }
    };
    const finish = (...before) => {
      node.append(...before, notice, actions);
      paintLimit();
      countdown = paintLimit;
      return node;
    };
    return { node, actions, finish, say: (message) => { notice.textContent = message; } };
  };
  // Answers every flow shares. Returns true when it took over.
  const common = (answer) => {
    const code = errorCode(answer);
    if (answer.status === 429 || code === "rate_limited") {
      const wait = Number(answer.data?.error?.details?.retry_after) || answer.retryAfter || 1;
      unlockCli = answer.data?.error?.details?.unlock_cli || SIGNIN_UNLOCK_CLI;
      limitedUntil = env.now() + wait * 1000;
      return true;
    }
    if (code === "gui_origin_unpinned") {
      viewNoUser(answer.data?.error?.details?.setup_cli);
      return true;
    }
    return false;
  };
  const guarded = async (send, say) => {
    let answer;
    try { answer = await send(); }
    catch { viewDown(); return null; }
    if (!answer.ok && common(answer)) return null;
    return answer;
  };

  // ── states without a form ──
  function viewDown() {
    const retry = button("Try again", true);
    retry.onclick = start;
    show("Sign-in is not available",
      text("The host authority that checks sign-in did not answer. On the host, start it with:"),
      cli(SIGNIN_START_CLI),
      text("If it is running, open the GUI on the address it was set up for: "
        + "127.0.0.1 and localhost are different addresses to a browser."),
      el("div", { className: "signin-actions" }, retry)).focus();
  }
  function viewRefused(error) {
    show("Sign-in is not available",
      text("The host authority refused this page"
        + (error ? `: ${[error.code, error.message].filter(Boolean).join(": ")}` : ".")),
      text("Open the GUI on the address it was set up for.")).focus();
  }
  function viewNoUser(setupCli) {
    show("No GUI user exists yet",
      text("On the host, create one, then reload this page:"),
      cli(setupCli || SIGNIN_SETUP_CLI)).focus();
  }
  function viewUnrecognised() {
    const retry = button("Try again", true);
    retry.onclick = start;
    const out = button("Sign out");
    out.onclick = async () => {
      try { await call(base, "/auth/sign-out", "POST", {}); } catch { /* shown by start() */ }
      start();
    };
    show("Signed in, but the GUI does not see it",
      text("The host authority accepted this browser, but the review server does not "
        + "recognise the session. Restarting the engine re-reads it:"),
      cli(SIGNIN_START_CLI),
      el("div", { className: "signin-actions" }, retry, out)).focus();
  }

  // After the authority accepts, wait until the review server sees the
  // session too, so the page never bounces between the two.
  async function finish() {
    enrolment = null;
    show("Signed in", text("Opening the GUI…")).focus();
    for (let attempt = 0; attempt < 8; attempt += 1) {
      try {
        const review = await call("", "/api/auth/status");
        if (review.ok && review.data.signed_in) { env.navigate(next); return; }
      } catch { /* try again */ }
      await env.sleep(250);
    }
    viewUnrecognised();
  }

  // ── sign-in ──
  function viewUsername(message = "") {
    enrolment = null;
    const f = form("Continue", async (say) => {
      const username = input.value.trim();
      if (!username) { say("Enter your username."); input.focus(); return; }
      const answer = await guarded(() => call(base, "/auth/identify", "POST", { username }), say);
      if (!answer) return;
      if (!answer.ok) { say(SIGNIN_FAILED); return; }
      if (answer.data.first_sign_in) viewClaim(username);
      else viewPassword(username);
    });
    const input = field(f.node, "username", "Username", {
      type: "text", autocomplete: "username", autocapitalize: "none", spellcheck: false,
      required: true,
    });
    show("Sign in", f.finish());
    if (message) f.say(message);
    input.focus();
  }
  const changeUser = () => {
    const back = el("button", { className: "signin-link", type: "button", textContent: "Use another username" });
    back.onclick = () => viewUsername();
    return back;
  };
  function viewPassword(username) {
    const f = form("Sign in", async (say) => {
      const digits = code.value.replace(/\s+/g, "");
      if (!password.value) { say("Enter your password."); password.focus(); return; }
      if (!/^\d{6}$/.test(digits)) { say("Enter the six-digit code from your authenticator app."); code.focus(); return; }
      const body = { username, password: password.value, code: digits };
      if (device.value.trim()) body.device = device.value.trim();
      const answer = await guarded(() => call(base, "/auth/sign-in", "POST", body), say);
      password.value = "";
      code.value = "";
      if (!answer) { password.focus(); return; }
      if (answer.ok && answer.data.signed_in) { await finish(); return; }
      say(SIGNIN_FAILED);
      password.focus();
    });
    field(f.node, "username", "Username", {
      type: "text", autocomplete: "username", value: username, readOnly: true,
    });
    const password = field(f.node, "password", "Password", {
      type: "password", autocomplete: "current-password", required: true,
    });
    const code = field(f.node, "code", "Authenticator code", {
      type: "text", autocomplete: "one-time-code", inputMode: "numeric", maxLength: 7,
      pattern: "[0-9 ]*", required: true,
    });
    const device = field(f.node, "device", "Device name (optional)", {
      type: "text", autocomplete: "off", maxLength: 64, placeholder: "For example: work laptop",
    });
    f.actions.append(changeUser());
    show("Sign in", f.finish());
    password.focus();
  }

  // ── first sign-in: claim code → password twice → link authenticator → one code ──
  function viewClaim(username, message = "") {
    enrolment = null;
    const f = form("Continue", async (say) => {
      const claim = input.value.trim();
      if (!claim) { say("Enter the claim code."); input.focus(); return; }
      const answer = await guarded(
        () => call(base, "/auth/first/begin", "POST", { username, claim_code: claim }), say);
      input.value = "";
      if (!answer) return;
      if (!answer.ok) { say("That claim code was not accepted."); input.focus(); return; }
      enrolment = answer.data;
      viewNewPassword(username);
    });
    const input = field(f.node, "claim", "Claim code", {
      type: "text", autocomplete: "off", autocapitalize: "characters", spellcheck: false,
      required: true,
    });
    f.node.append(el("div", { className: "signin-hint" },
      "Printed once by ./sc auth user add on the host."));
    f.actions.append(changeUser());
    show("First sign-in",
      text(`${username} has not signed in before. Enter the one-time claim code to set a `
        + "password and link an authenticator app."),
      f.finish());
    if (message) f.say(message);
    input.focus();
  }
  function viewNewPassword(username, message = "") {
    const minimum = Number(enrolment.password_min) || SIGNIN_PASSWORD_MIN;
    const f = form("Continue", async (say) => {
      if (first.value.length < minimum) {
        say(`Use at least ${minimum} characters.`); first.focus(); return;
      }
      if (first.value !== second.value) {
        say("The two passwords do not match."); second.value = ""; second.focus(); return;
      }
      viewLink(username, first.value);
    });
    field(f.node, "username", "Username", {
      type: "text", autocomplete: "username", value: username, readOnly: true,
    });
    const first = field(f.node, "new-password", "Choose a password", {
      type: "password", autocomplete: "new-password", minLength: minimum, required: true,
    });
    f.node.append(el("div", { className: "signin-hint" }, `At least ${minimum} characters.`));
    const second = field(f.node, "new-password-again", "Enter it again", {
      type: "password", autocomplete: "new-password", minLength: minimum, required: true,
    });
    show("Choose a password", f.finish());
    if (message) f.say(message);
    first.focus();
  }
  // Nothing has been sent since /auth/first/begin: the password and the code
  // go to the authority together, in the one confirm request below.
  function viewLink(username, password) {
    const held = enrolment;
    const f = form("Finish", async (say) => {
      const digits = code.value.replace(/\s+/g, "");
      if (!/^\d{6}$/.test(digits)) { say("Enter the six-digit code your authenticator app shows."); code.focus(); return; }
      const body = { enrolment: held.enrolment, password, code: digits };
      if (device.value.trim()) body.device = device.value.trim();
      const answer = await guarded(() => call(base, "/auth/first/confirm", "POST", body), say);
      code.value = "";
      if (!answer) { code.focus(); return; }
      if (answer.ok && answer.data.signed_in) { await finish(); return; }
      const reason = errorCode(answer);
      if (reason === "enrolment_expired")
        viewClaim(username, "First sign-in timed out. Enter the claim code again.");
      else if (reason === "password_unacceptable")
        viewNewPassword(username, "That password was not accepted. Choose another.");
      else {
        say("That code did not match. Wait for the next code and try again.");
        code.focus();
      }
    });
    const canvas = el("canvas", { className: "signin-qr", role: "img",
      ariaLabel: "QR code to scan with an authenticator app" });
    const drawn = drawSignInQr(canvas, held.otpauth_uri, env.qrcode);
    const code = field(f.node, "code", "Code from the app", {
      type: "text", autocomplete: "one-time-code", inputMode: "numeric", maxLength: 7,
      pattern: "[0-9 ]*", required: true,
    });
    const device = field(f.node, "device", "Device name (optional)", {
      type: "text", autocomplete: "off", maxLength: 64, placeholder: "For example: work laptop",
    });
    show("Link an authenticator app",
      text("Scan this code with an authenticator app, or enter the key by hand. "
        + "It is shown only now."),
      drawn ? canvas : text("The QR code could not be drawn. Enter the key by hand."),
      el("code", { className: "signin-key", ariaLabel: "Manual key" }, groupKey(held.secret)),
      text(`Account ${held.issuer || "Subfloor"}: ${held.username || username} · time-based, `
        + `${held.algorithm || "SHA1"}, ${held.digits || 6} digits, ${held.period || 30} s`),
      el("details", {}, el("summary", {}, "Setup link"),
        el("code", { className: "signin-uri" }, held.otpauth_uri)),
      f.finish());
    code.focus();
  }

  async function start() {
    show("Sign in", text("Loading…"));
    let review;
    try { review = await call("", "/api/auth/status"); } catch { review = null; }
    if (!review || !review.ok) {
      const retry = button("Try again", true);
      retry.onclick = start;
      show("Sign-in is not available", text("The review server did not answer."),
        el("div", { className: "signin-actions" }, retry)).focus();
      return;
    }
    base = signInAuthorityBase(review.data.authority?.url, location);
    let auth = null;
    if (base) {
      try { auth = await call(base, "/auth/status"); } catch { auth = null; }
    }
    if (!auth) {
      // Without the authority nobody can sign in, but a session that the
      // review server already accepts still works.
      if (review.data.signed_in) env.navigate(next);
      else viewDown();
      return;
    }
    if (!auth.ok) { viewRefused(auth.data.error); return; }
    const status = auth.data;
    if (status.pinned === false || !status.enrolled) { viewNoUser(status.setup_cli); return; }
    if (status.signed_in && review.data.signed_in) { env.navigate(next); return; }
    if (status.signed_in) { await finish(); return; }
    if (Number(status.retry_after) > 0) limitedUntil = env.now() + Number(status.retry_after) * 1000;
    viewUsername();
  }

  return { start, next };
}

if (typeof document !== "undefined" && document.getElementById("signin-body")) {
  createSignIn({
    root: document.getElementById("signin-body"),
    document, location: window.location,
    fetch: (url, init) => window.fetch(url, init),
    qrcode: window.qrcode,
    now: () => Date.now(),
    setInterval: (fn, ms) => window.setInterval(fn, ms),
    clearInterval: (id) => window.clearInterval(id),
    sleep: (ms) => new Promise((resolve) => window.setTimeout(resolve, ms)),
    navigate: (to) => window.location.replace(to),
  }).start();
}
