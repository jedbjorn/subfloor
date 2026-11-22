(() => {
  const trigger = document.querySelector("#paired-browsers");
  if (!trigger) return;

  const dialog = document.createElement("dialog");
  dialog.id = "browser-dialog";
  const heading = document.createElement("h2");
  heading.textContent = "Paired browsers";
  const explanation = document.createElement("p");
  explanation.className = "muted";
  explanation.textContent = "Each device has an independent long-lived session. Revoking one does not sign out the others.";
  const status = document.createElement("p");
  status.className = "notice";
  status.setAttribute("role", "status");
  const list = document.createElement("div");
  list.className = "session-list";
  const footer = document.createElement("footer");
  const signOut = document.createElement("button");
  signOut.type = "button";
  signOut.textContent = "Sign out this browser";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "primary";
  close.textContent = "Done";
  close.addEventListener("click", () => dialog.close());
  footer.append(signOut, close);
  dialog.append(heading, explanation, status, list, footer);
  document.body.append(dialog);

  async function call(path, options = {}) {
    const response = await fetch(path, {credentials: "same-origin", ...options});
    if (response.status === 401) {
      const next = encodeURIComponent(location.pathname + location.search + location.hash);
      location.replace("/signin.html?next=" + next);
      throw new Error("Sign-in required");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || data.error || "Request failed");
    return data;
  }

  function sessionCard(session) {
    const item = document.createElement("article");
    item.className = "session-item";
    const name = document.createElement("strong");
    name.textContent = session.device + (session.current ? " · this browser" : "");
    const times = document.createElement("small");
    times.textContent = `Last seen ${session.last_seen_at || "unknown"} · paired ${session.created_at || "unknown"}`;
    item.append(name, times);
    if (!session.current) {
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.textContent = "Revoke";
      revoke.addEventListener("click", async () => {
        if (revoke.dataset.confirm !== "yes") {
          revoke.dataset.confirm = "yes";
          revoke.textContent = "Confirm revoke";
          setTimeout(() => { revoke.dataset.confirm = ""; revoke.textContent = "Revoke"; }, 5000);
          return;
        }
        revoke.disabled = true;
        try {
          await call("/api/auth/sessions/revoke", {method: "POST",
            headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: session.id})});
          await load();
        } catch (error) {
          status.textContent = error.message;
          revoke.disabled = false;
        }
      });
      item.append(revoke);
    }
    return item;
  }

  async function load() {
    status.textContent = "Loading paired browsers…";
    list.replaceChildren();
    try {
      const data = await call("/api/auth/sessions");
      list.replaceChildren(...data.sessions.map(sessionCard));
      status.textContent = data.sessions.length ? "" : "No paired browsers.";
    } catch (error) {
      status.textContent = error.message;
    }
  }

  trigger.addEventListener("click", () => {
    dialog.showModal();
    load();
  });
  signOut.addEventListener("click", async () => {
    signOut.disabled = true;
    try {
      await call("/api/auth/sign-out", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
      location.replace("/signin.html");
    } catch (error) {
      status.textContent = error.message;
      signOut.disabled = false;
    }
  });

  call("/api/auth/status").then(data => { trigger.hidden = !data.session; }).catch(() => { trigger.hidden = true; });
})();
