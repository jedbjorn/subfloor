const NEXT_UNSAFE = /[\\\u0000-\u001f\u007f]/;
const NEXT_LOCAL = /^\/(?![\/\\])/;

function safeNext(raw) {
  if (typeof raw !== "string" || NEXT_UNSAFE.test(raw) || !NEXT_LOCAL.test(raw)) return "/";
  try {
    const url = new URL(raw, location.origin);
    const path = url.pathname + url.search + url.hash;
    if (url.origin !== location.origin || NEXT_UNSAFE.test(path) || !NEXT_LOCAL.test(path)) return "/";
    const again = new URL(path, location.origin);
    if (again.origin !== location.origin || again.pathname + again.search + again.hash !== path) return "/";
    return again.pathname === "/signin.html" ? "/" : path;
  } catch { return "/"; }
}

const next = safeNext(new URLSearchParams(location.search).get("next") || "/");
const form = document.querySelector("#pair-form");
const submit = document.querySelector("#pair-submit");
const notice = document.querySelector("#pair-status");

async function call(path, options = {}) {
  const response = await fetch(path, {credentials: "same-origin", ...options});
  const data = await response.json().catch(() => ({}));
  return {response, data};
}

function showError(message) {
  notice.textContent = message;
}

async function start() {
  submit.disabled = true;
  try {
    const {response, data} = await call("/api/auth/status");
    if (!response.ok) {
      showError("Manager sign-in is unavailable. Reload to try again.");
      return;
    }
    if (data.signed_in) {
      location.replace(location.origin + next);
      return;
    }
    if (data.enrolled === false) {
      const setup = document.querySelector("#pair-setup");
      setup.textContent = "Set the Manager password on Halo with ./sc manager password, then reload this page.";
      setup.hidden = false;
      form.hidden = true;
      return;
    }
    submit.disabled = Number(data.retry_after) > 0;
    if (submit.disabled) showError(`Too many failed attempts. Try again in ${data.retry_after} seconds.`);
  } catch {
    showError("Manager did not answer. Check the connection and reload.");
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  submit.disabled = true;
  showError("");
  const fields = new FormData(form);
  const password = fields.get("password");
  try {
    const {response, data} = await call("/api/auth/pair", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username: fields.get("username"), password,
        device: fields.get("device")}),
    });
    form.elements.password.value = "";
    if (response.ok && data.signed_in) {
      location.replace(location.origin + next);
      return;
    }
    const message = data.error?.message || data.error || "Pairing failed. Check the username and Manager password.";
    showError(typeof message === "string" ? message : "Pairing failed. Check the username and Manager password.");
    if (response.status === 429) {
      const retry = Number(response.headers.get("Retry-After")) || 1;
      setTimeout(() => { submit.disabled = false; }, retry * 1000);
      return;
    }
  } catch {
    form.elements.password.value = "";
    showError("Manager did not answer. Check the connection and try again.");
  }
  submit.disabled = false;
});

start();
