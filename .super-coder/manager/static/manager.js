const table = document.querySelector("table");
const status = document.querySelector("#status");
const refresh = document.querySelector("#refresh");
let expandedMenu;
let menuSequence = 0;

function cell(row, value, label) {
  const td = row.insertCell();
  td.dataset.label = label;
  td.textContent = value;
  return td;
}

function closeMenu(returnFocus = false) {
  if (!expandedMenu) return;
  const {trigger, menu} = expandedMenu;
  menu.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
  expandedMenu = null;
  if (returnFocus) trigger.focus();
}

function showMenu(trigger, menu, last = false) {
  closeMenu();
  expandedMenu = {trigger, menu};
  menu.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
  const items = [...menu.querySelectorAll('[role="menuitem"]')];
  items[last ? items.length - 1 : 0]?.focus();
}

function actionMenu(row, installation) {
  const actions = cell(row, "", "Actions");
  actions.className = "actions";
  const trigger = button("⋯", () => {});
  trigger.className = "menu-trigger";
  const label = `Actions for ${installation.name}`;
  trigger.setAttribute("aria-label", label);
  trigger.setAttribute("aria-haspopup", "menu");
  trigger.setAttribute("aria-expanded", "false");
  const id = `repository-menu-${++menuSequence}`;
  trigger.setAttribute("aria-controls", id);
  const menu = document.createElement("div");
  menu.id = id;
  menu.className = "action-menu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", label);
  menu.hidden = true;
  const lifecycle = installation.state === "stopped" ? ["start"] :
    installation.state === "running" ? ["stop", "restart"] : [];
  const update = ["running", "stopped"].includes(installation.state) ? ["update"] : [];
  for (const action of [...lifecycle, ...update, "remove"]) {
    const item = button(titles[action], () => {
      closeMenu();
      preview({name: installation.name, action});
    });
    item.setAttribute("role", "menuitem");
    item.tabIndex = -1;
    if (action === "remove") item.className = "danger";
    menu.append(item);
  }
  trigger.addEventListener("click", event => {
    event.stopPropagation();
    if (expandedMenu?.trigger === trigger) closeMenu(true);
    else showMenu(trigger, menu);
  });
  trigger.addEventListener("keydown", event => {
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      showMenu(trigger, menu, event.key === "ArrowUp");
    }
  });
  menu.addEventListener("keydown", event => {
    const items = [...menu.querySelectorAll('[role="menuitem"]')];
    const current = items.indexOf(document.activeElement);
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
    } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 :
        (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    } else if (event.key === "Tab") {
      closeMenu(true);
    }
  });
  actions.append(trigger, menu);
  return actions;
}

document.addEventListener("click", event => {
  if (expandedMenu && !expandedMenu.menu.contains(event.target) && event.target !== expandedMenu.trigger) closeMenu();
});

function installationRow(installation) {
  const row = document.createElement("tr");
  row.dataset.repo = installation.name;
  const repo = cell(row, "", "Repository");
  const local = location.hostname === "127.0.0.1";
  const url = installation.open_url || (local ? installation.local_url : null);
  const name = document.createElement(url ? "a" : "span");
  name.className = "repository-name";
  if (url) {
    name.classList.add("repository-link");
    name.href = url;
    name.target = "_blank";
    name.rel = "noopener noreferrer";
  }
  name.textContent = installation.name;
  const path = document.createElement("small");
  path.className = "repository-path";
  path.textContent = installation.path;
  repo.append(name, path);
  const observations = [["Runtime", installation.runtime], ["Service", installation.state],
    ["GUI health", installation.health], ["Publication", installation.publication]];
  for (const [label, value] of observations) {
    const td = cell(row, "", label);
    const badge = document.createElement("span");
    badge.className = "badge";
    if (["running", "healthy", "verified"].includes(value)) badge.classList.add("good");
    if (["failed", "unavailable", "identity mismatch", "invalid port"].includes(value)) badge.classList.add("bad");
    if (["starting", "stopping", "paused", "pending"].includes(value)) badge.classList.add("warn");
    badge.textContent = value === "docker" ? "Docker" : value[0].toUpperCase() + value.slice(1);
    td.append(badge);
  }
  if (installation.configured_runtime !== "unknown" && installation.configured_runtime !== installation.runtime) {
    const selection = document.createElement("small");
    selection.textContent = `Configured for ${installation.configured_runtime === "host" ? "Host" : "Docker"}`;
    row.cells[1].append(selection);
  }
  if (installation.published_url) {
    const address = document.createElement("small");
    address.textContent = installation.published_url;
    row.cells[4].append(address);
  }
  actionMenu(row, installation);
  return row;
}

const titles = {start: "Start", stop: "Stop", restart: "Restart", update: "Update Subfloor",
  remove: "Remove Subfloor from this repo", create: "Create repository", install: "Install Subfloor"};
const setup = document.querySelector("#setup");
const setupStatus = document.querySelector("#setup-status");
const previewButton = document.querySelector("#preview-install");
const review = document.querySelector("#review");
const provision = document.querySelector("#provision-progress");
const provisionStatus = document.querySelector("#provision-status");
const provisionOutput = document.querySelector("#provision-output");
const provisionIndicator = provision.querySelector("progress");
const provisionClose = document.querySelector("#provision-close");
const phrase = document.querySelector("#phrase");
const confirmation = phrase.querySelector("input");
const applyButton = document.querySelector("#apply");
let pending;
let refreshTimer;
let loading;
let refreshQueued = false;
let results = [];
const accepted = new Set();

function button(label, handle) {
  const el = document.createElement("button");
  el.textContent = label;
  el.type = "button";
  el.addEventListener("click", handle);
  return el;
}

async function request(url, payload, timeout) {
  const options = payload ? {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)} : {cache: "no-store"};
  if (timeout) options.signal = AbortSignal.timeout(timeout);
  const response = await fetch(url, options);
  if (response.status === 401) {
    const next = encodeURIComponent(location.pathname + location.search + location.hash);
    location.replace("/signin.html?next=" + next);
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function preview(data, installation = false) {
  try {
    const message = installation ? setupStatus : status;
    message.hidden = false;
    message.textContent = "Checking target…";
    pending = await request("/api/actions/preview", data);
    if (installation && !setup.open) return;
    if (pending.existing) {
      if (setup.open) setup.close();
      await load();
      const row = [...table.rows].find(row => row.dataset.repo === pending.existing);
      const open = row?.querySelector("a");
      if (open) location.assign(open.href);
      else status.textContent = `${pending.existing} is installed. Check its service and publication status.`;
      return;
    }
    document.querySelector("#review-title").textContent = titles[pending.action];
    document.querySelector("#plan").textContent = pending.detail;
    phrase.hidden = !pending.phrase;
    phrase.querySelector("span").textContent = `Type ${pending.phrase} to confirm removal`;
    confirmation.value = "";
    applyButton.disabled = Boolean(pending.phrase);
    if (setup.open) setup.close();
    review.showModal();
    status.textContent = "";
  } catch (error) {
    (installation ? setupStatus : status).textContent = error.message;
  }
}

document.querySelector("#provision").onclick = () => {
  setupStatus.hidden = true;
  setup.showModal();
};
for (const el of document.querySelectorAll("[data-close]")) el.onclick = () => el.closest("dialog").close();
document.querySelector("#setup-form").onsubmit = async event => {
  event.preventDefault();
  if (previewButton.disabled) return;
  previewButton.disabled = true;
  try {
    await preview(Object.fromEntries(new FormData(event.target)), true);
  } finally {
    previewButton.disabled = false;
  }
};
confirmation.oninput = () => { applyButton.disabled = confirmation.value !== pending.phrase; };
provision.addEventListener("cancel", event => {
  if (provisionClose.disabled) event.preventDefault();
});

async function provisionRepository({name, id, action}) {
  review.close();
  document.querySelector("#provision-title").textContent = `${titles[action]} · ${name}`;
  provisionStatus.textContent = "Starting installation…";
  provisionOutput.hidden = true;
  provisionClose.disabled = true;
  provisionIndicator.hidden = false;
  provision.showModal();
  provisionStatus.focus();

  try {
    let record;
    let stage = "Starting installation";
    try {
      record = await request("/api/actions/apply", {name, id}, 15000);
      accepted.add(id);
      load();
    } catch (error) {
      if (error.status && error.status < 500) throw error;
      provisionStatus.textContent = "Checking whether the Manager started the installation…";
    }

    while (!record || record.status === "running") {
      if (record) {
        stage = record.stage || stage;
        const message = `${stage}…`;
        if (provisionStatus.textContent !== message) provisionStatus.textContent = message;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
      try {
        record = await request("/api/actions/status/" + id, undefined, 10000);
      } catch (error) {
        if (error.status === 404) throw new Error("The Manager could not confirm this operation. Check Repository operations before retrying.");
        provisionStatus.textContent = `Last step was “${stage}”. Reconnecting to the Manager…`;
        record = null;
      }
    }

    if (record.status === "succeeded") {
      provisionStatus.textContent = "Subfloor is installed and running.";
    } else {
      const outcome = record.status === "interrupted" ? "Installation interrupted" : "Installation failed";
      provisionStatus.textContent = [outcome, record.stage, record.detail].filter(Boolean).join(" · ");
      provisionOutput.textContent = "Loading command output…";
      provisionOutput.hidden = false;
      provisionIndicator.hidden = true;
      try {
        const {output} = await request("/api/actions/output/" + id, undefined, 10000);
        provisionOutput.textContent = output || "No command output.";
      } catch {
        provisionOutput.textContent = "Command output is unavailable. You can try again from Repository operations.";
      }
    }
  } catch (error) {
    provisionStatus.textContent = error.message;
  } finally {
    accepted.delete(id);
    provisionIndicator.hidden = true;
    provisionClose.disabled = false;
    provisionClose.focus();
    load();
  }
}

document.querySelector("#apply-form").onsubmit = async event => {
  event.preventDefault();
  if (applyButton.disabled) return;
  applyButton.disabled = true;
  if (["create", "install"].includes(pending.action)) {
    await provisionRepository(pending);
    return;
  }
  try {
    const receipt = await request("/api/actions/apply", {name: pending.name, id: pending.id, confirmation: confirmation.value});
    accepted.add(receipt.id);
    review.close();
    await load();
  } catch (error) {
    review.close();
    await load();
    status.textContent = error.message;
  }
};

function showResults(results) {
  document.querySelector("#history").hidden = !results.length;
  const list = document.querySelector("#results");
  list.replaceChildren(...results.map(record => {
    const item = document.createElement("article");
    const name = document.createElement("strong");
    name.textContent = `${titles[record.action]} · ${record.status}`;
    const detail = document.createElement("small");
    detail.textContent = [record.path, record.stage, record.detail].filter(Boolean).join(" · ");
    item.append(name, detail);
    for (const line of record.recovery || []) {
      const recovery = document.createElement("small");
      recovery.textContent = line;
      item.append(recovery);
    }
    item.append(button("View output", async () => {
      try {
        const data = await request("/api/actions/output/" + record.id);
        const log = document.querySelector("#log");
        log.querySelector("pre").textContent = data.output || record.detail || "No command output yet.";
        log.showModal();
      } catch (error) { status.textContent = error.message; }
    }));
    return item;
  }));
}

// Allow several 30-second publisher scans after each completed provision.
function awaitingPublication(installations) {
  const cutoff = Date.now() / 1000 - 120;
  const byPath = new Map(installations.map(row => [row.path, row.publication]));
  const seen = new Set();
  for (const record of results) {
    if (!["create", "install", "remove"].includes(record.action) || record.status !== "succeeded") continue;
    if (seen.has(record.path)) continue;
    seen.add(record.path);
    if (record.action !== "remove" && (record.finished ?? record.created) >= cutoff &&
        !["verified", "not configured"].includes(byPath.get(record.path))) return true;
  }
  return false;
}

async function loadOnce() {
  clearTimeout(refreshTimer);
  refresh.disabled = true;
  status.textContent = "Checking installations…";
  let installations = [];
  try {
    results = await request("/api/actions");
    showResults(results);
    const data = await request("/api/inventory");
    installations = data.installations;
    document.querySelector("#root").textContent = data.root;
    document.querySelector("#analytics").hidden = !data.analytics_available;
    closeMenu();
    table.tBodies[0].replaceChildren(...installations.map(installationRow));
    table.hidden = !installations.length;
    status.textContent = table.hidden ? "No installations found." : "";
    for (const record of results) {
      if (record.status !== "running") accepted.delete(record.id);
    }
  } catch (error) {
    table.hidden = true;
    status.textContent = error.message;
  } finally {
    refresh.disabled = false;
    if (accepted.size || results.some(record => record.status === "running") || awaitingPublication(installations)) {
      refreshTimer = setTimeout(load, 3000);
    }
  }
}

function load() {
  if (loading) {
    refreshQueued = true;
    return loading;
  }
  loading = (async () => {
    do {
      refreshQueued = false;
      await loadOnce();
    } while (refreshQueued);
    loading = null;
  })();
  return loading;
}
refresh.addEventListener("click", load);
load();
