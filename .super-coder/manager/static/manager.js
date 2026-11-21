const table = document.querySelector("table");
const status = document.querySelector("#status");
const refresh = document.querySelector("#refresh");

function cell(row, value) {
  const td = row.insertCell();
  td.textContent = value;
  return td;
}

function installationRow(installation) {
  const row = document.createElement("tr");
  row.dataset.repo = installation.name;
  const repo = cell(row, installation.name);
  const path = document.createElement("small");
  path.textContent = installation.path;
  repo.append(path);
  for (const value of [installation.runtime, installation.state, installation.health, installation.publication]) {
    const td = cell(row, value === "docker" ? "Docker" : value[0].toUpperCase() + value.slice(1));
    if (["running", "healthy", "verified"].includes(value)) td.classList.add("good");
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
  const actions = cell(row, "");
  actions.className = "actions";
  const local = location.hostname === "127.0.0.1";
  const url = installation.open_url || (local ? installation.local_url : null);
  if (url) {
    const open = document.createElement("a");
    open.href = url;
    open.textContent = "Open";
    actions.append(open);
  }
  const controls = document.createElement("div");
  controls.className = "controls";
  const lifecycle = installation.state === "stopped" ? ["start"] :
    installation.state === "running" ? ["stop", "restart"] : [];
  for (const action of [...lifecycle, "remove"]) {
    controls.append(button(titles[action], () => preview({name: installation.name, action})));
  }
  actions.append(controls);
  return row;
}

const titles = {start: "Start", stop: "Stop", restart: "Restart", remove: "Remove Subfloor from this repo",
  create: "Create repository", install: "Install Subfloor"};
const setup = document.querySelector("#setup");
const review = document.querySelector("#review");
const phrase = document.querySelector("#phrase");
const confirmation = phrase.querySelector("input");
const applyButton = document.querySelector("#apply");
let pending;
let refreshTimer;
let loading = false;

function button(label, handle) {
  const el = document.createElement("button");
  el.textContent = label;
  el.type = "button";
  el.addEventListener("click", handle);
  return el;
}

async function request(url, payload) {
  const options = payload ? {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)} : {cache: "no-store"};
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function preview(data) {
  try {
    status.textContent = "Checking target…";
    pending = await request("/api/actions/preview", data);
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
    if (setup.open) setup.close();
    status.textContent = error.message;
  }
}

document.querySelector("#provision").onclick = () => setup.showModal();
for (const el of document.querySelectorAll("[data-close]")) el.onclick = () => el.closest("dialog").close();
document.querySelector("#setup-form").onsubmit = event => {
  event.preventDefault();
  preview(Object.fromEntries(new FormData(event.target)));
};
confirmation.oninput = () => { applyButton.disabled = confirmation.value !== pending.phrase; };
document.querySelector("#apply-form").onsubmit = async event => {
  event.preventDefault();
  applyButton.disabled = true;
  try {
    await request("/api/actions/apply", {name: pending.name, id: pending.id, confirmation: confirmation.value});
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
    item.append(name, detail, button("View output", async () => {
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

async function load() {
  if (loading) return;
  loading = true;
  clearTimeout(refreshTimer);
  refresh.disabled = true;
  status.textContent = "Checking installations…";
  try {
    const results = await request("/api/actions");
    const data = await request("/api/inventory");
    showResults(results);
    if (results.some(record => record.status === "running") ||
        (data.installations.some(row => row.publication === "pending") &&
         results.some(record => ["create", "install"].includes(record.action) && record.status === "succeeded"))) {
      refreshTimer = setTimeout(load, 3000);
    }
    document.querySelector("#root").textContent = data.root;
    document.querySelector("#analytics").hidden = !data.analytics_available;
    table.tBodies[0].replaceChildren(...data.installations.map(installationRow));
    table.hidden = !data.installations.length;
    status.textContent = table.hidden ? "No installations found." : "";
  } catch (error) {
    table.hidden = true;
    status.textContent = error.message;
  } finally {
    refresh.disabled = false;
    loading = false;
  }
}
refresh.addEventListener("click", load);
load();
