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
// Funnel order: idea inlet → most-active committed work → done.
const STATUSES = ["brainstorm", "in_progress", "next", "near_term", "long_term", "shipped"];
const SLABEL = { brainstorm: "Brainstorm", in_progress: "In Progress", next: "Next", near_term: "Near Term", long_term: "Long Term", shipped: "Shipped" };
const roadmapFilter = new Set();   // empty = show all (default)

async function renderRoadmap(root) {
  const { buckets } = await api("/roadmap");
  root.replaceChildren();

  // grouped status toggle-filters; none selected = all shown
  const bar = el("div", { className: "filters" });
  for (const s of STATUSES) {
    const chip = el("button", { className: "chip" + (roadmapFilter.has(s) ? " on" : ""), textContent: SLABEL[s] });
    chip.onclick = () => {
      roadmapFilter.has(s) ? roadmapFilter.delete(s) : roadmapFilter.add(s);
      renderRoadmap(root);
    };
    bar.append(chip);
  }
  root.append(bar);

  // buckets arrive linear from the API; filter to the selected statuses
  const shown = roadmapFilter.size ? buckets.filter((b) => roadmapFilter.has(b.status)) : buckets;
  if (!shown.length) { root.append(el("div", { className: "muted" }, "No features in the selected stage(s).")); return; }
  for (const b of shown) {
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

// A document row: the primary action OPENS it rendered in md-converter (the
// markdown rides in the URL via /open → ?c=). No inline raw-markdown expand.
// Non-frozen docs get an explicit "edit" toggle; frozen ones are read-only.
function docBlock(d) {
  const wrap = el("div", { className: "docrow" });
  const label = `${d.kind} v${d.seq}${d.frozen ? " · frozen " + (d.frozen_date || "") : ""}: ${d.title || ""}`;
  const open = el("a", {
    className: "act primary", href: "/api/documents/" + d.document_id + "/open",
    target: "_blank", rel: "noopener", textContent: "open in md-converter ↗",
  });
  const head = el("div", { className: "docrow-head" }, el("span", { className: "docrow-label" }, label), open);
  wrap.append(head);

  if (!d.frozen) {
    const box = el("div", { hidden: true });
    const ta = el("textarea", { rows: 14 });
    const save = el("button", { className: "act primary", textContent: "save doc" });
    save.onclick = async () => {
      try { await api("/documents/" + d.document_id, "PATCH", { body: ta.value }); setStatus("doc saved"); }
      catch (e) { toast("error: " + e.message); }
    };
    const edit = el("button", { className: "act", textContent: "edit" });
    edit.onclick = async () => {
      box.hidden = !box.hidden;
      if (!box.hidden && !ta.dataset.loaded) {
        const full = await api("/documents/" + d.document_id);
        ta.value = full.body || ""; ta.dataset.loaded = "1";
      }
    };
    head.append(edit);
    box.append(ta, save);
    wrap.append(box);
  } else {
    wrap.append(el("div", { className: "lock-note", textContent: "frozen — read-only. Open the next spec, don't edit this one." }));
  }
  return wrap;
}

// ── Docs ──────────────────────────────────────────────────────────────────────
async function renderDocs(root) {
  const { docs } = await api("/docs");
  root.replaceChildren();
  root.append(el("div", { className: "muted" },
    "Documentation (kind=doc), separate from the spec dev-cycle on the Roadmap. Open renders in md-converter."));
  if (!docs.length) {
    root.append(el("div", { className: "card muted" },
      "No docs yet. A doc is a kind='doc' document against a feature — authored by the shell, viewable here."));
    return;
  }
  const byFeat = {};
  for (const d of docs) (byFeat[d.feature_title || "— unlinked —"] ||= []).push(d);
  for (const [title, list] of Object.entries(byFeat)) {
    const c = el("div", { className: "card" });
    c.append(el("h2", {}, title));
    for (const d of list) c.append(docBlock(d));
    root.append(c);
  }
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

// ── Scripts ─────────────────────────────────────────────────────────────────────
async function renderScripts(root) {
  const { scripts } = await api("/scripts");
  root.replaceChildren();
  root.append(el("div", { className: "muted" },
    "Run a maintenance script. Output appears below it. Per-instance DB edits → run Snapshot, then Render flat, to persist them to git-tracked text."));
  for (const s of scripts) {
    const c = el("div", { className: "card" });
    const h = el("h2", {}, s.name);
    if (s.danger) h.append(el("span", { className: "pill warn", textContent: " danger" }));
    c.append(h, el("div", { className: "muted" }, s.desc));
    const out = el("pre", { className: "doc-body", hidden: true });
    const run = el("button", { className: "act" + (s.danger ? "" : " primary"), textContent: "run" });
    run.onclick = async () => {
      if (s.danger && !confirm(`Run "${s.name}"?\n\n${s.desc}`)) return;
      run.disabled = true; setStatus("running " + s.key + "…");
      try {
        const r = await fetch("/api/scripts/" + s.key, { method: "POST" });
        const data = await r.json();
        out.hidden = false; out.textContent = data.output || "(done)";
        setStatus(data.ok ? s.key + " ✓" : s.key + " failed (" + data.code + ")");
      } catch (e) { out.hidden = false; out.textContent = "error: " + e.message; }
      finally { run.disabled = false; }
    };
    c.append(run, out);
    root.append(c);
  }
}

// ── Tabs + boot ────────────────────────────────────────────────────────────────
const VIEWS = {
  shells: ["#view-shells", renderShells],
  roadmap: ["#view-roadmap", renderRoadmap],
  docs: ["#view-docs", renderDocs],
  flags: ["#view-flags", renderFlags],
  scripts: ["#view-scripts", renderScripts],
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
