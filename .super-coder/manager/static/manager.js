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
  return row;
}

async function load() {
  refresh.disabled = true;
  status.textContent = "Checking installations…";
  try {
    const response = await fetch("/api/inventory", {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Inventory request failed (${response.status})`);
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
  }
}
refresh.addEventListener("click", load);
load();
