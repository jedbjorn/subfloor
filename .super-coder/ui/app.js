// super-coder review UI — vanilla JS, no build step. Talks to the same-origin
// stdlib API. Read everything; edit only what the laws and freeze rules allow.

const $ = (s, r = document) => r.querySelector(s);
const el = (t, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(t), props);
  for (const k of kids) n.append(k?.nodeType ? k : document.createTextNode(k ?? ""));
  return n;
};
const esc = (s) => (s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

async function api(path, method = "GET", body) {
  const r = await fetch("/api" + path, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toast(msg) {
  const t = el("div", { className: "toast" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 4000);
}
function setStatus(s) { $("#status").textContent = s; }

// md-converter deep-link: the spec reuses ?c= (gzip+base64url). We don't have the
// encoder client-side, so v1 links to the app with the doc title as a hint;
// full ?c= encoding lands when the GUI shares superCC's Python encoder.
const MDC = "https://md-converter.designs-os.com";

// ── Shells ──────────────────────────────────────────────────────────────────
async function renderShells(root) {
  const { shells } = await api("/shells");
  root.replaceChildren();
  for (const s of shells) {
    const full = await api("/shells/" + s.shell_id);
    root.append(shellCard(full));
  }
}

function field(label, value, key, sid) {
  const ta = el("textarea", { value: value || "", rows: key === "current_state" ? 4 : 2 });
  const save = el("button", { className: "act", textContent: "save" });
  save.onclick = async () => {
    try { await api("/shells/" + sid, "PATCH", { [key]: ta.value }); setStatus("saved " + key); }
    catch (e) { toast("error: " + e.message); }
  };
  return el("div", {}, el("label", { className: "k", textContent: label }), ta, save);
}

function shellCard(s) {
  const c = el("div", { className: "card" });
  c.append(el("h2", {}, `${s.display_name} `, el("span", { className: "muted", textContent: "/" + (s.shortname || "") })));
  c.append(el("div", { className: "muted" }, `${s.role || ""} — ${s.mandate || ""}`));

  // editable operational fields
  c.append(field("current_state", s.current_state, "current_state", s.shell_id));
  c.append(field("connections", s.connections, "connections", s.shell_id));
  c.append(field("workspace", s.workspace, "workspace", s.shell_id));

  // skills + grants (editable)
  const sk = el("div", {});
  sk.append(el("label", { className: "k", textContent: "skills + grants" }));
  for (const k of s.skills) {
    const cb = el("input", { type: "checkbox", checked: !!k.granted });
    cb.onchange = async () => {
      try { await api(`/shells/${s.shell_id}/skills/${k.skill_id}`, "PUT", { granted: cb.checked }); setStatus("grant updated"); }
      catch (e) { toast("error: " + e.message); cb.checked = !cb.checked; }
    };
    sk.append(el("div", { className: "list-skill" }, cb, el("b", {}, k.name),
      el("span", { className: "muted", textContent: " — " + (k.description || "").split("\n")[0] })));
  }
  c.append(sk);

  // seed + L&S — READ ONLY (no edit endpoint exists; law-curated)
  if (s.seed?.length) {
    const box = el("div", { className: "locked" });
    box.append(el("label", { className: "k", textContent: "seed (read-only — shell-curated, Laws 2–4)" }));
    for (const e of s.seed) box.append(el("div", { className: "seed-entry" },
      el("div", { className: "d", textContent: e.entry_date }), el("div", {}, e.body)));
    c.append(box);
  }
  if (s.lns?.length) {
    const box = el("div", { className: "locked" });
    box.append(el("label", { className: "k", textContent: "lessons & stances (read-only — Law 7)" }));
    for (const e of s.lns) box.append(el("div", { className: "lns-entry" }, e.body));
    c.append(box);
  }
  if (s.lineage_seed) {
    const d = el("details", {}, el("summary", {}, "lineage seed (read-only)"));
    d.append(el("div", { className: "locked seed-entry" }, s.lineage_seed));
    c.append(d);
  }
  return c;
}

// ── Roadmap ───────────────────────────────────────────────────────────────────
const STATUSES = ["brainstorm", "long_term", "near_term", "next", "shipped"];

async function renderRoadmap(root) {
  const { buckets } = await api("/roadmap");
  root.replaceChildren();
  for (const b of buckets) {
    const sec = el("div", { className: "bucket" }, el("h2", {}, b.label));
    for (const f of b.features) sec.append(featureCard(f));
    root.append(sec);
  }
}

function featureCard(f) {
  const c = el("div", { className: "card" });
  const head = el("h2", {}, f.title || "(untitled)");
  if (f.owner) head.append(el("span", { className: "pill " + f.roadmap_status, textContent: " " + f.owner }));
  c.append(head);

  // editable: title / status / summary / sort
  const title = el("input", { type: "text", value: f.title || "" });
  const status = el("select", {});
  for (const s of STATUSES) status.append(el("option", { value: s, selected: s === f.roadmap_status, textContent: s }));
  const summary = el("textarea", { value: f.summary || "", rows: 2 });
  const save = el("button", { className: "act", textContent: "save feature" });
  save.onclick = async () => {
    try { await api("/roadmap/" + f.feature_id, "PATCH", { title: title.value, roadmap_status: status.value, summary: summary.value }); setStatus("feature saved"); load("roadmap"); }
    catch (e) { toast("error: " + e.message); }
  };
  c.append(el("div", { className: "grid2" },
    el("span", { className: "k" }, "title"), title,
    el("span", { className: "k" }, "status"), status,
    el("span", { className: "k" }, "summary"), summary), save);

  // documents — tabbed; non-frozen editable, frozen read-only
  for (const d of f.documents || []) c.append(docBlock(d));

  // open flags = blockers
  if (f.open_flags?.length) {
    const fl = el("div", {});
    fl.append(el("label", { className: "k", textContent: "blockers (open flags)" }));
    for (const x of f.open_flags) fl.append(el("div", { className: "tag" }, `${x.display_name || ""} ${x.description || ""}`));
    c.append(fl);
  }
  return c;
}

function docBlock(d) {
  const wrap = el("div", {});
  const label = `${d.kind} v${d.seq}${d.frozen ? " · frozen " + (d.frozen_date || "") : ""}: ${d.title || ""}`;
  const det = el("details", {}, el("summary", {}, label));
  const ta = el("textarea", { rows: 12 });
  const open = el("button", { className: "act", textContent: "open in md-converter ↗" });
  open.onclick = () => window.open(MDC, "_blank");
  det.ontoggle = async () => {
    if (!det.open || ta.dataset.loaded) return;
    const full = await api("/documents/" + d.document_id);
    ta.value = full.body || ""; ta.dataset.loaded = "1";
  };
  det.append(ta);
  if (d.frozen) {
    ta.readOnly = true;
    det.append(el("div", { className: "lock-note", textContent: "frozen — read-only. Open the next spec, don't edit this one." }), open);
  } else {
    const save = el("button", { className: "act primary", textContent: "save doc" });
    save.onclick = async () => {
      try { await api("/documents/" + d.document_id, "PATCH", { body: ta.value }); setStatus("doc saved"); }
      catch (e) { toast("error: " + e.message); }
    };
    det.append(el("div", { className: "row" }, save, open));
  }
  wrap.append(det);
  return wrap;
}

// ── Flags ──────────────────────────────────────────────────────────────────────
async function renderFlags(root) {
  const { flags, features } = await api("/flags");
  root.replaceChildren();

  // create
  const card = el("div", { className: "card" });
  card.append(el("h2", {}, "New flag"));
  const name = el("input", { type: "text", placeholder: "display name (e.g. SC-001)" });
  const desc = el("input", { type: "text", placeholder: "[Area] description | Blocker for: …" });
  const feat = el("select", {});
  feat.append(el("option", { value: "", textContent: "— no feature —" }));
  for (const f of features) feat.append(el("option", { value: f.feature_id, textContent: f.title }));
  const prio = el("select", {});
  for (const p of ["High", "Medium", "Low"]) prio.append(el("option", { value: p, selected: p === "Medium", textContent: p }));
  const create = el("button", { className: "act primary", textContent: "create flag" });
  create.onclick = async () => {
    if (!desc.value) return toast("description required");
    try {
      await api("/flags", "POST", { display_name: name.value || null, description: desc.value, feature_id: feat.value || null, priority: prio.value });
      setStatus("flag created"); load("flags");
    } catch (e) { toast("error: " + e.message); }
  };
  card.append(el("div", { className: "grid2" },
    el("span", { className: "k" }, "name"), name,
    el("span", { className: "k" }, "description"), desc,
    el("span", { className: "k" }, "feature"), feat,
    el("span", { className: "k" }, "priority"), prio), create);
  root.append(card);

  // grouped by feature
  const byFeat = {};
  for (const f of flags) (byFeat[f.feature_title || "— unlinked —"] ||= []).push(f);
  for (const [title, list] of Object.entries(byFeat)) {
    const c = el("div", { className: "card" });
    c.append(el("h2", {}, title));
    for (const f of list) c.append(flagRow(f));
    root.append(c);
  }
}

function flagRow(f) {
  const row = el("div", { className: "flag" + (f.resolved ? " resolved" : "") });
  row.append(el("span", { className: "pill " + (f.priority || "").toLowerCase() }, f.priority || ""));
  const d = el("div", { className: "desc" });
  d.append(el("b", {}, (f.display_name ? f.display_name + " " : "")), esc(f.description || ""));
  if (f.resolved) d.append(el("div", { className: "tag" }, `resolved ${f.resolved_date || ""} — ${f.resolution_notes || ""}`));
  row.append(d);
  if (!f.resolved) {
    const btn = el("button", { className: "act", textContent: "resolve" });
    btn.onclick = async () => {
      const notes = prompt("Resolution notes:");
      if (notes === null) return;
      try { await api("/flags/" + f.flag_id, "PATCH", { resolved: 1, resolution_notes: notes }); setStatus("flag resolved"); load("flags"); }
      catch (e) { toast("error: " + e.message); }
    };
    row.append(btn);
  }
  return row;
}

// ── Tabs + boot ────────────────────────────────────────────────────────────────
const VIEWS = {
  shells: ["#view-shells", renderShells],
  roadmap: ["#view-roadmap", renderRoadmap],
  flags: ["#view-flags", renderFlags],
};
async function load(tab) {
  const [sel, fn] = VIEWS[tab];
  try { await fn($(sel)); } catch (e) { $(sel).replaceChildren(el("div", { className: "card" }, "error: " + e.message)); }
}
function show(tab) {
  for (const b of document.querySelectorAll("nav button")) b.classList.toggle("active", b.dataset.tab === tab);
  for (const k of Object.keys(VIEWS)) $(VIEWS[k][0]).hidden = k !== tab;
  load(tab);
}
document.querySelectorAll("nav button").forEach((b) => (b.onclick = () => show(b.dataset.tab)));
$("#snapshot").onclick = async () => {
  setStatus("snapshotting…");
  try { const r = await api("/snapshot", "POST"); toast(r.output || "done"); setStatus("snapshot done"); }
  catch (e) { toast("error: " + e.message); }
};
(async () => {
  try { const h = await api("/health"); $("#repo").textContent = h.repo; setStatus("port " + h.port); }
  catch { setStatus("offline"); }
  show("shells");
})();
