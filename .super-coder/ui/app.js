// super-coder review UI — vanilla JS, no build step. Talks to the same-origin
// stdlib API. Read everything; edit only what the laws and freeze rules allow.

const $ = (s, r = document) => r.querySelector(s);
const el = (t, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(t), props);
  for (const k of kids) n.append(k?.nodeType ? k : document.createTextNode(k ?? ""));
  return n;
};
const esc = (s) => (s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// Markdown → sanitized HTML via the vendored marked + DOMPurify (the same
// pipeline as dos-arch's MarkdownBlock). External links open in a new tab
// with rel=noopener; the hook is global to the DOMPurify singleton, so it is
// registered exactly once.
marked.setOptions({ gfm: true, breaks: true });
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName !== "A" || !node.hasAttribute("href")) return;
  const href = node.getAttribute("href");
  if (/^https?:\/\//i.test(href) && !href.startsWith(window.location.origin)) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  } else {
    node.removeAttribute("target");
  }
});
function mdBlock(text) {
  const div = el("div", { className: "md" });
  if (text) div.innerHTML = DOMPurify.sanitize(
    marked.parse(String(text)), { USE_PROFILES: { html: true } });
  return div;
}

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

// ── Skill sections ──────────────────────────────────────────────────────────
// One grouping rule for the Skills tab AND the Shells grant list. "Repo skills"
// are fork-local (origin='repo', derived server-side from the snapshot rule:
// name not under engine assets/skills) and always lead; engine skills section
// by their category.
const SECTION_ORDER = ["repo", "substrate", "craft"];
const SECTION_LABEL = { repo: "Repo skills", substrate: "Substrate", craft: "Craft", other: "Other" };
const SECTION_NOTE = {
  repo: "Authored in this repo — not engine catalogue. Durable via .sc-state/content.sql; see the local_skill_management skill.",
};
const sectionOf = (s) => (s.origin === "repo" ? "repo" : (s.category || "other"));
const sectionLabel = (k) => SECTION_LABEL[k] || k.charAt(0).toUpperCase() + k.slice(1);

function groupSkills(skills, { alwaysRepo = false } = {}) {
  const by = {};
  if (alwaysRepo) by.repo = [];   // surface the section even when empty
  for (const s of skills) (by[sectionOf(s)] ||= []).push(s);
  const keys = [
    ...SECTION_ORDER.filter((k) => k in by),
    ...Object.keys(by).filter((k) => !SECTION_ORDER.includes(k)).sort(),
  ];
  return keys.map((k) => ({ key: k, label: sectionLabel(k), skills: by[k] }));
}

// ── Shells ──────────────────────────────────────────────────────────────────
// dos-arch-style viewer (ported from dos-arch shell_core/ui /shells): sticky
// identity sub-header (pill shell picker + role/mandate), then Harness |
// Skills sub-tabs scoped to the selected shell. Flat panels, accordions,
// popover pickers, and a unified edit modal.
let selectedShell = null;
let shellTab = "harness";     // 'harness' | 'skills'
let activeSkillId = null;     // skill-viewer selection; reset on shell switch

// Rough token estimator — BPE-ish, ~15% off for English; the tilde in the
// readout makes the approximation explicit. No bundled tokenizer.
const approxTokens = (s) => Math.ceil((s || "").length / 4);
const fmt = (n) => n.toLocaleString();
const microlabel = (text) => el("span", { className: "microlabel" }, text);

function statRow(pairs) {
  const r = el("div", { className: "stat-row" });
  for (const [k, v] of pairs) r.append(el("span", { className: "stat" }, k + " ", el("b", {}, v)));
  return r;
}

// On/off switch — a styled checkbox; onChange gets (next, input) so a failed
// write can flip the control back.
function toggleSwitch(checked, onChange) {
  const cb = el("input", { type: "checkbox", checked });
  cb.onchange = () => onChange(cb.checked, cb);
  return el("label", { className: "switch" }, cb, el("span", { className: "slider" }));
}

// Vanilla port of dos-arch's GlassDropdown: pill trigger + solid-grey popover.
// One document-level mousedown handler (registered at boot) closes any open
// .gmenu the click landed outside of.
function glassDropdown({ items, value, onChange }) {
  const wrap = el("div", { className: "gdrop" });
  const cur = items.find((i) => i.value === value);
  const btn = el("button", { className: "gdrop-btn", type: "button" });
  btn.append(el("span", { className: "gdrop-label" }, cur ? cur.label : "—"),
    el("span", { className: "gdrop-caret" }, "⇅"));
  // gmenu-fit: the menu matches the trigger's width (long labels ellipsize)
  const menu = el("div", { className: "gmenu gmenu-fit", hidden: true });
  for (const it of items) {
    const row = el("button", { className: "gmenu-row" + (it.value === value ? " active-row" : ""), type: "button" });
    row.append(el("span", { className: "gmenu-name" }, it.label));
    if (it.caption) row.append(el("span", { className: "gmenu-cap" }, it.caption));
    row.onclick = () => { menu.hidden = true; onChange(it.value); };
    menu.append(row);
  }
  btn.onclick = () => { menu.hidden = !menu.hidden; };
  wrap.append(btn, menu);
  return wrap;
}

// Modal base (dos-arch dialog): overlay click or Esc closes; header carries
// the title + an optional readout; footer nodes sit space-between. Returns
// the close function.
function openModal({ title, headExtra, bodyNode, footNodes, width = 650, height = 700 }) {
  const overlay = el("div", { className: "modal-overlay" });
  const close = () => overlay.remove();
  overlay.onmousedown = (e) => { if (e.target === overlay) close(); };
  const dlg = el("div", { className: "modal" });
  dlg.style.width = width + "px";
  dlg.style.height = height + "px";
  const head = el("div", { className: "modal-head" }, el("div", { className: "modal-title" }, title));
  if (headExtra) head.append(headExtra);
  dlg.append(head, el("div", { className: "modal-body" }, bodyNode));
  if (footNodes?.length) dlg.append(el("div", { className: "modal-foot" }, ...footNodes));
  overlay.append(dlg);
  document.body.append(overlay);
  return close;
}

// Unified edit modal — 650×700, Save bottom-LEFT / Cancel bottom-RIGHT,
// live ~tokens / chars readout in the header.
function openEditModal({ title, value, onSave }) {
  const counter = el("div", { className: "modal-count" });
  const ta = el("textarea", { value: value || "" });
  const upd = () => { counter.textContent = `~${fmt(approxTokens(ta.value))} tokens / ${fmt(ta.value.length)} chars`; };
  ta.oninput = upd; upd();
  const save = el("button", { className: "act primary", type: "button", textContent: "Save" });
  const cancel = el("button", { className: "act", type: "button", textContent: "Cancel" });
  const close = openModal({ title, headExtra: counter, bodyNode: ta, footNodes: [save, cancel] });
  save.onclick = async () => {
    save.disabled = true; save.textContent = "Saving…";
    try { await onSave(ta.value); close(); }
    catch (e) { toast("error: " + e.message); save.disabled = false; save.textContent = "Save"; }
  };
  cancel.onclick = close;
  ta.focus();
}

// Read-only skill-content viewer — 800×650, rendered markdown with a raw
// toggle bottom-left, char/~token readout in the header.
async function openSkillContentModal(skill) {
  try {
    const full = await api("/skills/" + skill.skill_id);
    const counter = el("div", { className: "modal-count" },
      `~${fmt(approxTokens(full.content || ""))} tokens / ${fmt((full.content || "").length)} chars`);
    const body = el("div", { className: "modal-md" });
    const rendered = mdBlock(full.content || "(no content)");
    const raw = el("pre", { className: "raw-pre", hidden: true }, full.content || "");
    body.append(rendered, raw);
    const rawBtn = el("button", { className: "act", type: "button", textContent: "raw" });
    rawBtn.onclick = () => {
      raw.hidden = !raw.hidden;
      rendered.hidden = !raw.hidden;
      rawBtn.textContent = raw.hidden ? "raw" : "rendered";
    };
    const closeBtn = el("button", { className: "act", type: "button", textContent: "Close" });
    const close = openModal({
      title: skill.name, headExtra: counter, bodyNode: body,
      footNodes: [rawBtn, closeBtn],
      width: 800, height: 650,
    });
    closeBtn.onclick = close;
  } catch (e) { toast("error: " + e.message); }
}

// New-shell form in a 600×300 modal — Create bottom-left, Cancel bottom-right,
// same dialog pattern as the new-flag modal.
function openNewShellModal(templates, root) {
  const fl = el("select", {});
  for (const t of templates)
    fl.append(el("option", { value: t.flavor, textContent: `${t.flavor} — ${t.role}` }));
  const nm = el("input", { type: "text", placeholder: "name (e.g. Arch)" });
  const create = el("button", { className: "act primary", type: "button", textContent: "Create" });
  const cancel = el("button", { className: "act", type: "button", textContent: "Cancel" });
  const form = el("div", { className: "modal-form" },
    el("span", { className: "k" }, "flavor"), fl,
    el("span", { className: "k" }, "name"), nm);
  const close = openModal({ title: "New shell", bodyNode: form,
    footNodes: [create, cancel], width: 600, height: 300 });
  create.onclick = async () => {
    if (!nm.value.trim()) return toast("name required");
    create.disabled = true; create.textContent = "Creating…";
    try {
      const r = await api("/shells", "POST", { flavor: fl.value, name: nm.value.trim() });
      selectedShell = r.shell_id; activeSkillId = null;
      close(); setStatus(`shell created — ${r.shortname}`); renderShells(root);
    } catch (e) { toast("error: " + e.message); create.disabled = false; create.textContent = "Create"; }
  };
  cancel.onclick = close;
  nm.focus();
}

async function renderShells(root) {
  const { shells } = await api("/shells");
  const { templates } = await api("/shell-templates");
  root.replaceChildren();
  if (!shells.length) { root.append(el("div", { className: "card muted" }, "No shells.")); return; }
  if (selectedShell == null || !shells.find((s) => s.shell_id === selectedShell))
    selectedShell = shells[0].shell_id;
  const s = await api("/shells/" + selectedShell);

  // sticky identity sub-header
  const sub = el("div", { className: "subbar" });
  const idy = el("div", { className: "subbar-id" });
  idy.append(glassDropdown({
    items: shells.map((x) => ({
      value: x.shell_id, label: x.display_name,
      caption: x.shortname ? "/" + x.shortname : (x.flavor || ""),
    })),
    value: selectedShell,
    onChange: (v) => { selectedShell = Number(v); activeSkillId = null; renderShells(root); },
  }));
  if (s.role) idy.append(el("div", { className: "kv" }, microlabel("Role"), el("span", {}, s.role)));
  if (s.mandate) idy.append(el("div", { className: "kv" }, microlabel("Mandate"), el("span", {}, s.mandate)));
  sub.append(idy);

  // new shell — modal trigger
  const newBtn = el("button", { className: "act", type: "button", textContent: "＋ New shell" });
  newBtn.onclick = () => openNewShellModal(templates, root);
  sub.append(newBtn);
  root.append(sub);

  // sub-tabs — Harness / Skills, both scoped to the selected shell
  const tabs = el("div", { className: "vtabs" });
  for (const [key, label] of [["harness", "Harness"], ["skills", "Skills"]]) {
    const b = el("button", { className: shellTab === key ? "active-tab" : "", type: "button", textContent: label });
    b.onclick = () => { shellTab = key; renderShells(root); };
    tabs.append(b);
  }
  root.append(tabs);

  const pane = el("div", { className: "shell-pane" });
  root.append(pane);
  if (shellTab === "harness") renderHarness(pane, s);
  else renderSkillViewer(pane, s);
}

// Harness — the shell's surfaces as grouped accordions: Operational
// (current_state is the one editable field — the API exposes nothing else),
// then the law-curated identity (read-only by design, Laws 2–4 / 7), then the
// record. Char/token readout spans everything below it.
function renderHarness(root, s) {
  const groups = [{ title: "Operational", items: [
    { label: "CURRENT STATE", text: s.current_state || "", editable: true },
    ...(s.system_prompt ? [{ label: "SYSTEM PROMPT", text: s.system_prompt }] : []),
  ] }];

  const idy = [];
  if (s.seed?.length) idy.push({
    label: `SEED (${s.seed.length})`,
    text: s.seed.map((e) => e.body).join("\n"),
    node: entryList(s.seed.map((e) => ({ d: e.entry_date, body: e.body }))),
  });
  if (s.lns?.length) idy.push({
    label: `LESSONS & STANCES (${s.lns.length})`,
    text: s.lns.map((e) => e.body).join("\n"),
    node: entryList(s.lns.map((e) => ({ body: e.body }))),
  });
  if (s.lineage_seed) idy.push({ label: "LINEAGE SEED", text: s.lineage_seed });
  if (idy.length) groups.push({ title: "Identity — law-curated, read-only", items: idy });

  if (s.decisions?.length) groups.push({ title: "Record", items: [{
    label: `RECENT DECISIONS (${s.decisions.length})`,
    text: s.decisions.map((e) => e.decision).join("\n"),
    node: entryList(s.decisions.map((e) => ({
      d: `${e.decision_date || ""} ${e.priority || ""}`.trim(), body: e.decision }))),
  }] });

  const all = groups.flatMap((g) => g.items);
  root.append(
    el("div", { className: "viewer-head" }, microlabel("Harness")),
    statRow([["Char Count", fmt(all.reduce((n, x) => n + x.text.length, 0))],
             ["Est. Tokens", "~" + fmt(approxTokens(all.map((x) => x.text).join("")))]]));

  const panel = el("div", { className: "vpanel acc-panel" });
  for (const g of groups) {
    panel.append(el("div", { className: "acc-group" }, g.title));
    for (const sec of g.items) panel.append(accordion(sec, s));
  }
  root.append(panel);
}

function entryList(entries) {
  const box = el("div", {});
  for (const e of entries) box.append(el("div", { className: "seed-entry" },
    ...(e.d ? [el("div", { className: "d", textContent: e.d })] : []),
    mdBlock(e.body)));
  return box;
}

function accordion(sec, s) {
  const d = el("details", { className: "acc" });
  d.append(el("summary", {}, el("span", { className: "acc-label" }, sec.label)));
  const body = el("div", { className: "acc-body" });
  if (sec.editable) {
    const pen = el("button", { className: "pencil", type: "button", title: "Edit current_state", textContent: "✎" });
    pen.onclick = () => openEditModal({
      title: "current_state — " + s.display_name,
      value: s.current_state,
      onSave: async (v) => {
        await api("/shells/" + s.shell_id, "PATCH", { current_state: v });
        setStatus("saved current_state"); load("shells");
      },
    });
    body.append(pen);
  }
  body.append(sec.node || (sec.text ? mdBlock(sec.text) : el("div", { className: "acc-text" }, "—")));
  d.append(body);
  return d;
}

// Skill Viewer — popover picker with inline grant toggles (☑/☐ — toggling
// does not change the selection), then the selected skill's full content in a
// panel with a char/token readout. Content lazy-loads per selection.
function renderSkillViewer(root, s) {
  const skills = s.skills;
  if (!skills.length) { root.append(el("div", { className: "muted" }, "No skills in the catalogue.")); return; }
  if (activeSkillId == null || !skills.find((k) => k.skill_id === activeSkillId))
    activeSkillId = (skills.find((k) => k.granted) || skills[0]).skill_id;
  const active = skills.find((k) => k.skill_id === activeSkillId);

  const wrap = el("div", { className: "gdrop" });
  const btn = el("button", { className: "gdrop-btn", type: "button" });
  btn.append(el("span", { className: "gdrop-label mono" }, active.name),
    el("span", { className: "gdrop-caret" }, "⇅"));
  const menu = el("div", { className: "gmenu", hidden: true });
  for (const k of skills) {
    const row = el("div", { className: "gmenu-item" + (k.skill_id === activeSkillId ? " active-row" : "") });
    const tog = el("button", { className: "gmenu-check", type: "button",
      title: k.granted ? "Revoke" : "Grant", textContent: k.granted ? "☑" : "☐" });
    tog.onclick = async () => {
      try {
        await api(`/shells/${s.shell_id}/skills/${k.skill_id}`, "PUT", { granted: !k.granted });
        k.granted = k.granted ? 0 : 1;
        tog.textContent = k.granted ? "☑" : "☐";
        tog.title = k.granted ? "Revoke" : "Grant";
        setStatus("grant updated");
      } catch (e) { toast("error: " + e.message); }
    };
    const sel = el("button", { className: "gmenu-name mono", type: "button", textContent: k.name });
    sel.onclick = () => {
      activeSkillId = k.skill_id; menu.hidden = true;
      root.replaceChildren(); renderSkillViewer(root, s);
    };
    row.append(tog, sel, el("span", { className: "gmenu-cap" }, sectionLabel(sectionOf(k))));
    menu.append(row);
  }
  btn.onclick = () => { menu.hidden = !menu.hidden; };
  wrap.append(btn, menu);

  // rendered markdown by default; the right-aligned toggle shows raw text
  const rawBtn = el("button", { className: "rawtoggle", type: "button",
    title: "Toggle raw markdown", textContent: "raw", hidden: true });
  root.append(el("div", { className: "viewer-head" }, microlabel("Skill Viewer"), wrap, rawBtn));
  const stats = statRow([["Char Count", "…"], ["Est. Tokens", "…"]]);
  const panel = el("div", { className: "vpanel viewer-panel" });
  root.append(stats, panel);

  api("/skills/" + activeSkillId).then((full) => {
    stats.replaceWith(statRow([
      ["Char Count", fmt((full.content || "").length)],
      ["Est. Tokens", "~" + fmt(approxTokens(full.content || ""))]]));
    if (full.description) panel.append(el("div", { className: "muted desc-line" }, full.description));
    const rendered = mdBlock(full.content || "(no content)");
    const raw = el("pre", { className: "raw-pre", hidden: true }, full.content || "");
    panel.append(rendered, raw);
    rawBtn.hidden = false;
    rawBtn.onclick = () => {
      raw.hidden = !raw.hidden;
      rendered.hidden = !raw.hidden;
      rawBtn.textContent = raw.hidden ? "raw" : "rendered";
    };
  }).catch((e) => panel.append(el("div", { className: "muted" }, "error: " + e.message)));
}

// ── Skills (catalogue, sectioned) ────────────────────────────────────────────
async function renderSkills(root) {
  const { skills, shells } = await api("/skills");
  root.replaceChildren();
  root.append(el("div", { className: "muted" },
    "The skills catalogue, sectioned. Engine skills ship with super-coder and group by category; repo skills are authored in this fork. Grants are editable here and on each shell."));
  for (const sec of groupSkills(skills, { alwaysRepo: true })) {
    const wrap = el("div", { className: "bucket" });
    const h = el("h2", {}, `${sec.label} `, el("span", { className: "count" }, String(sec.skills.length)));
    wrap.append(h);
    if (SECTION_NOTE[sec.key]) wrap.append(el("div", { className: "muted note" }, SECTION_NOTE[sec.key]));
    if (!sec.skills.length) {
      wrap.append(el("div", { className: "card muted" },
        "No repo skills yet — author one with the local_skill_management skill (file → seed → grant → snapshot)."));
      root.append(wrap);
      continue;
    }
    const card = el("div", { className: "card skills" });
    for (const s of sec.skills) card.append(skillRow(s, shells));
    wrap.append(card);
    root.append(wrap);
  }
}

function skillRow(s, shells) {
  const row = el("details", { className: "skill" });
  // collapsed row stays quiet: mono name + truncated description, no badges —
  // origin/section is the group header, grants live in the expanded body
  const head = el("summary", { className: "skill-head" });
  head.append(
    el("b", { className: "skill-name mono" }, s.name),
    el("span", { className: "muted desc", textContent: (s.description || "").split("\n")[0] }));
  row.append(head);

  const body = el("div", { className: "skill-body" });
  if (s.command) body.append(el("div", { className: "tag" }, "command: ", el("code", {}, s.command)));

  // grants — every available shell as a row with an on/off toggle; same PUT
  // the Shells tab uses, managed from the skill's side here
  const gr = el("div", { className: "grants" });
  gr.append(el("label", { className: "k", textContent: "granted to" }));
  const list = el("div", { className: "grant-list" });
  for (const sh of shells) {
    const sw = toggleSwitch(s.granted_shells.includes(sh.shell_id), async (next, cb) => {
      try {
        await api(`/shells/${sh.shell_id}/skills/${s.skill_id}`, "PUT", { granted: next });
        setStatus("grant updated");
        const i = s.granted_shells.indexOf(sh.shell_id);
        if (next && i < 0) s.granted_shells.push(sh.shell_id);
        if (!next && i >= 0) s.granted_shells.splice(i, 1);
      } catch (e) { toast("error: " + e.message); cb.checked = !next; }
    });
    list.append(el("div", { className: "grant-row" },
      sw,
      el("span", { className: "grant-name" }, sh.display_name,
        el("span", { className: "muted", textContent: sh.shortname ? " /" + sh.shortname : "" }))));
  }
  gr.append(list);
  body.append(gr);

  // full procedure body opens in the viewer modal (800×650)
  const view = el("button", { className: "act", textContent: "view content" });
  view.onclick = () => openSkillContentModal(s);
  body.append(view);
  row.append(body);
  return row;
}

// ── Roadmap ───────────────────────────────────────────────────────────────────
// Board order: delivered first, then the committed funnel backward (items move
// LEFT toward shipped); brainstorm/retired are the right-hand end caps.
const STATUSES = ["shipped", "in_progress", "next", "near_term", "long_term", "brainstorm", "retired"];
const SLABEL = { brainstorm: "Brainstorm", in_progress: "In Progress", next: "Next", near_term: "Near Term", long_term: "Long Term", shipped: "Shipped", retired: "Retired" };
// The five stages that sequence (carry dependency edges). brainstorm/retired are
// excluded from the Flow graph and the blocker editor — they don't relate yet.
const FLOW_STAGES = ["in_progress", "next", "near_term", "long_term", "shipped"];
let roadmapFilter = null;            // null = show all (default); single-select
let roadmapView = "board";           // "board" | "flow"
const roadmapCollapsed = new Set();  // statuses whose section is collapsed

// All features in the sequencing stages, flattened — the candidate pool for a
// feature's "blocked by" picker and the node set of the Flow graph.
function flowCandidates(buckets) {
  const out = [];
  for (const b of buckets) if (FLOW_STAGES.includes(b.status))
    for (const f of b.features) out.push(f);
  return out;
}

async function renderRoadmap(root) {
  const { buckets, projects = [] } = await api("/roadmap");
  root.replaceChildren();

  // Board ⇄ Flow toggle. The sub-view rides in the URL hash (#roadmap = board,
  // #roadmap-flow = flow) so it's deep-linkable and refresh-stable; routeFromHash
  // sets roadmapView and re-renders.
  const toggle = el("div", { className: "filters centered view-toggle" });
  for (const [mode, label] of [["board", "Board"], ["flow", "Flow"]]) {
    const b = el("button", { className: "chip" + (roadmapView === mode ? " on" : ""), textContent: label });
    b.onclick = () => { location.hash = mode === "flow" ? "roadmap-flow" : "roadmap"; };
    toggle.append(b);
  }
  root.append(toggle);

  if (roadmapView === "flow") { await renderRoadmapFlow(root, buckets, projects); return; }

  const candidates = flowCandidates(buckets);

  // separated pill filters, centered; re-click the active one to clear → show all
  const bar = el("div", { className: "filters centered" });
  for (const s of STATUSES) {
    const chip = el("button", { className: "chip" + (roadmapFilter === s ? " on" : ""), textContent: SLABEL[s] });
    chip.onclick = () => {
      roadmapFilter = roadmapFilter === s ? null : s;
      renderRoadmap(root);
    };
    bar.append(chip);
  }
  root.append(bar);

  // buckets arrive linear from the API; filter to the single selected status.
  // The Board is a workload-per-horizon view (status sections); work-stream
  // grouping lives in the Flow view, not here.
  const shown = roadmapFilter ? buckets.filter((b) => b.status === roadmapFilter) : buckets;
  if (!shown.length) { root.append(el("div", { className: "muted" }, "No features in the selected stage.")); return; }
  for (const b of shown) {
    const sec = el("div", { className: "bucket" + (roadmapCollapsed.has(b.status) ? " collapsed" : "") });
    const h = el("h2", {}, b.label);
    h.onclick = () => {
      roadmapCollapsed.has(b.status) ? roadmapCollapsed.delete(b.status) : roadmapCollapsed.add(b.status);
      renderRoadmap(root);
    };
    sec.append(h);
    for (const f of b.features) sec.append(featureCard(f, candidates, projects));
    root.append(sec);
  }
}

// Flow view: one section per work-stream (project). Inside a section the
// work-stream's features lay out left→right by planning stage (the sequence),
// and an SVG overlay wires dependencies (prerequisite → dependent). Pure DOM +
// measured coordinates — no diagram library. Work-streams are the user's
// "feature" (e.g. "Meeting Intelligence" = the mi-capture project); unassigned
// features collect in a trailing "Ungrouped" section.
const SVGNS = "http://www.w3.org/2000/svg";
async function renderRoadmapFlow(root, buckets, projects = []) {
  const feats = flowCandidates(buckets);   // features in the sequencing stages
  if (!feats.length) {
    root.append(el("div", { className: "muted" }, "No features in the sequencing stages yet."));
    return;
  }
  const stageOf = {};
  for (const b of buckets) if (FLOW_STAGES.includes(b.status))
    for (const f of b.features) stageOf[f.feature_id] = b.status;

  // Group the sequencing features by work-stream (project_id; null = ungrouped).
  const byProj = new Map();   // key (project_id | null) → { title, features }
  for (const f of feats) {
    const key = f.project_id ?? null;
    if (!byProj.has(key)) byProj.set(key, { title: f.project_title || null, features: [] });
    byProj.get(key).features.push(f);
  }
  const order = projects.map((p) => p.project_id).filter((id) => byProj.has(id));
  for (const key of byProj.keys()) if (key !== null && !order.includes(key)) order.push(key);
  if (byProj.has(null)) order.push(null);

  let anyEdge = false;
  for (const key of order) {
    const grp = byProj.get(key);
    const title = key === null ? "Ungrouped" : (grp.title || ("project #" + key));
    const section = el("div", { className: "flow-stream" });
    section.append(el("h2", { className: "flow-stream-head" }, title));
    const { wrap, edges } = buildFlowGraph(grp.features, stageOf);
    anyEdge = anyEdge || edges > 0;
    section.append(wrap);
    root.append(section);
  }

  root.append(el("div", { className: "muted flow-hint" }, anyEdge
    ? "Wires run prerequisite → dependent (what must come first). Set a feature's “depends on” in Board view."
    : "No dependencies set — wire one by opening a feature in Board view and setting its “depends on”."));
}

// Build one work-stream's graph: stage columns scoped to `features`, plus an SVG
// overlay wiring dependency edges (prerequisite → dependent) whose endpoints are
// both in this set. Returns { wrap element, edges count }.
function buildFlowGraph(features, stageOf) {
  const shownIds = new Set(features.map((f) => f.feature_id));
  const wrap = el("div", { className: "flow-wrap" });
  const inner = el("div", { className: "flow-inner" });
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("class", "flow-wires");
  const cols = el("div", { className: "flow-cols" });

  const cardOf = {};   // feature_id → card element, for wire endpoints
  for (const s of FLOW_STAGES) {
    const inStage = features.filter((f) => stageOf[f.feature_id] === s);
    if (!inStage.length) continue;
    const col = el("div", { className: "flow-col" });
    col.append(el("div", { className: "flow-col-head" }, SLABEL[s]));
    for (const f of inStage) {
      const card = el("div", { className: "flow-card " + s });
      card.dataset.fid = String(f.feature_id);
      card.append(el("div", { className: "flow-card-title" }, f.title || "(untitled)"));
      const m = el("div", { className: "flow-card-meta" });
      if (f.owner) m.append(el("span", { className: "pill " + s }, f.owner));
      if (f.open_flags?.length) m.append(el("span", { className: "pill warn" }, f.open_flags.length + " ⚑"));
      if (m.childNodes.length) card.append(m);
      // md-converter open-links, one per spec/doc (same /open redirect the Board
      // card uses). Compact: "spec v1 ↗" / "doc ↗".
      const docs = f.documents || [];
      if (docs.length) {
        const dl = el("div", { className: "flow-card-docs" });
        for (const d of docs) dl.append(el("a", {
          className: "flow-doc-link", href: "/api/documents/" + d.document_id + "/open",
          target: "_blank", rel: "noopener",
          textContent: (d.kind === "doc" ? "doc" : `${d.kind} v${d.seq}`) + " ↗" }));
        card.append(dl);
      }
      col.append(card);
      cardOf[f.feature_id] = card;
    }
    cols.append(col);
  }

  inner.append(svg, cols);
  wrap.append(inner);

  // Dependency edges (prerequisite → dependent), endpoints both in this section.
  // Skip edges whose prerequisite is already shipped: shipped is the rightmost
  // stage, so the wire would point backward to earlier work — and a done
  // prerequisite isn't worth drawing.
  const edgeList = [];
  for (const f of features) for (const b of (f.blockers || []))
    if (shownIds.has(b) && stageOf[b] !== "shipped") edgeList.push([b, f.feature_id]);

  // Draw once the columns have laid out. Coordinates are relative to .flow-inner;
  // connect the source card's right edge to the target's left, horizontal-tangent.
  const draw = () => {
    if (!inner.isConnected) return;
    const base = inner.getBoundingClientRect();
    const w = inner.scrollWidth, h = inner.scrollHeight;
    svg.setAttribute("width", w); svg.setAttribute("height", h);
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    const arrow = document.createElementNS(SVGNS, "marker");
    arrow.setAttribute("id", "flowArrow");
    arrow.setAttribute("viewBox", "0 0 8 8");
    arrow.setAttribute("refX", "7"); arrow.setAttribute("refY", "4");
    arrow.setAttribute("markerWidth", "6"); arrow.setAttribute("markerHeight", "6");
    arrow.setAttribute("orient", "auto-start-reverse");
    const head = document.createElementNS(SVGNS, "path");
    head.setAttribute("d", "M0 0 L8 4 L0 8 z");
    head.setAttribute("fill", "context-stroke");
    arrow.append(head);
    const defs = document.createElementNS(SVGNS, "defs");
    defs.append(arrow);
    svg.replaceChildren(defs);
    for (const [from, to] of edgeList) {
      const a = cardOf[from], z = cardOf[to];
      if (!a || !z) continue;
      const ra = a.getBoundingClientRect(), rz = z.getBoundingClientRect();
      const x1 = ra.right - base.left, y1 = ra.top - base.top + ra.height / 2;
      const x2 = rz.left  - base.left, y2 = rz.top - base.top + rz.height / 2;
      const dx = Math.max(40, Math.abs(x2 - x1) * 0.4);
      const path = document.createElementNS(SVGNS, "path");
      path.setAttribute("d", `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`);
      path.setAttribute("class", "flow-wire");
      path.setAttribute("marker-end", "url(#flowArrow)");
      path.dataset.from = String(from); path.dataset.to = String(to);
      svg.append(path);
    }
  };
  requestAnimationFrame(draw);

  // Redraw on resize; the listener removes itself once this section is replaced.
  const onResize = () => { inner.isConnected ? draw() : window.removeEventListener("resize", onResize); };
  window.addEventListener("resize", onResize);

  // Hover a card → spotlight its incident wires and the cards they touch.
  for (const card of Object.values(cardOf)) {
    card.onmouseenter = () => {
      const fid = card.dataset.fid;
      const lit = new Set([fid]);
      wrap.classList.add("flow-hover");
      for (const p of svg.querySelectorAll(".flow-wire")) {
        const on = p.dataset.from === fid || p.dataset.to === fid;
        p.classList.toggle("lit", on);
        if (on) { lit.add(p.dataset.from); lit.add(p.dataset.to); }
      }
      for (const id in cardOf) cardOf[id].classList.toggle("lit", lit.has(id));
    };
    card.onmouseleave = () => {
      wrap.classList.remove("flow-hover");
      for (const p of svg.querySelectorAll(".flow-wire")) p.classList.remove("lit");
      for (const id in cardOf) cardOf[id].classList.remove("lit");
    };
  }

  return { wrap, edges: edgeList.length };
}

function featureCard(f, candidates = [], projects = []) {
  // Expandable box: collapsed shows title + status/owner pills + a one-line
  // summary preview; expanded reveals the editable fields, docs, and blockers.
  const c = el("details", { className: "card feature" });
  // Spec tasks (implementation plan) → side-bar colour: all done = green,
  // any still open = sunset orange. No tasks = no side bar.
  const tasks = f.tasks || [];
  const doneCount = tasks.filter((t) => t.status === "done").length;
  if (tasks.length) c.classList.add("has-tasks", doneCount === tasks.length ? "tasks-done" : "tasks-open");
  const sum = el("summary", { className: "feature-head" });
  sum.append(el("span", { className: "feature-title" }, f.title || "(untitled)"));
  const meta = el("span", { className: "feature-meta" });
  meta.append(el("span", { className: "pill " + f.roadmap_status, textContent: SLABEL[f.roadmap_status] || f.roadmap_status }));
  if (f.owner) meta.append(el("span", { className: "pill " + f.roadmap_status, textContent: f.owner }));
  if (f.open_flags?.length) meta.append(el("span", { className: "pill warn", textContent: f.open_flags.length + " ⚑" }));
  sum.append(meta);
  c.append(sum);
  if (f.summary) c.append(el("div", { className: "feature-preview muted" }, f.summary));

  const body = el("div", { className: "feature-body" });

  // editable: title / status / summary / sort
  const title = el("input", { type: "text", value: f.title || "" });
  const status = el("select", {});
  for (const s of STATUSES) status.append(el("option", { value: s, selected: s === f.roadmap_status, textContent: s }));
  const summary = el("textarea", { value: f.summary || "", rows: 7 });

  // project (work-stream) picker — drives the Board's grouping. Options: none,
  // each active work-stream, then "＋ new…" which creates one inline (POST) and
  // selects it without a reload, so unsaved title/summary edits survive.
  const project = el("select", { className: "project-select" });
  const NEW = "__new__";
  project.append(el("option", { value: "", selected: !f.project_id, textContent: "— none —" }));
  for (const p of projects) project.append(el("option", {
    value: String(p.project_id), selected: p.project_id === f.project_id, textContent: p.title }));
  const newOpt = el("option", { value: NEW, textContent: "＋ new work-stream…" });
  project.append(newOpt);
  let prevProject = project.value;
  project.onchange = async () => {
    if (project.value !== NEW) { prevProject = project.value; return; }
    const title = (prompt("New work-stream name:") || "").trim();
    if (!title) { project.value = prevProject; return; }
    try {
      const p = await api("/projects", "POST", { title });
      const opt = el("option", { value: String(p.project_id), textContent: p.title });
      project.insertBefore(opt, newOpt);
      project.value = String(p.project_id);
      prevProject = project.value;
    } catch (e) { project.value = prevProject; toast("error: " + e.message); }
  };

  // "depends on" editor — a multi-select of OTHER sequencing-stage features this
  // one must come after (stored as blocker edges; the Flow view wires them).
  // Only shown for the five real stages; brainstorm/retired don't relate yet.
  const realStage = FLOW_STAGES.includes(f.roadmap_status);
  let blockerSelect = null;
  if (realStage) {
    const others = candidates.filter((c) => c.feature_id !== f.feature_id);
    if (others.length) {
      blockerSelect = el("select", { multiple: true, className: "blocker-select",
        size: Math.min(6, others.length) });
      const cur = new Set(f.blockers || []);
      for (const c of others) blockerSelect.append(el("option", {
        value: String(c.feature_id), selected: cur.has(c.feature_id),
        textContent: c.title || ("#" + c.feature_id) }));
    }
  }

  const save = el("button", { className: "act", textContent: "save feature" });
  save.onclick = async () => {
    try {
      await api("/roadmap/" + f.feature_id, "PATCH",
                { title: title.value, roadmap_status: status.value, summary: summary.value,
                  project_id: project.value && project.value !== NEW ? Number(project.value) : null });
      if (blockerSelect) {
        const ids = [...blockerSelect.selectedOptions].map((o) => Number(o.value));
        await api("/roadmap/" + f.feature_id + "/blockers", "PUT", { blocked_by: ids });
      }
      setStatus("feature saved"); load("roadmap");
    } catch (e) { toast("error: " + e.message); }
  };
  const gridKids = [
    el("span", { className: "k" }, "title"), title,
    el("span", { className: "k" }, "status"), status,
    el("span", { className: "k" }, "project"), project,
    el("span", { className: "k" }, "summary"), summary,
  ];
  if (blockerSelect) gridKids.push(
    el("span", { className: "k" }, "depends on"), blockerSelect);
  body.append(el("div", { className: "grid2" }, ...gridKids), save);

  // tasks — the spec's implementation plan, in order; done = checked + struck
  if (tasks.length) {
    body.append(el("label", { className: "k", textContent: `tasks (${doneCount}/${tasks.length})` }));
    const ul = el("ul", { className: "task-list" });
    for (const t of tasks) {
      const done = t.status === "done";
      const li = el("li", { className: done ? "done" : (t.status === "in_progress" ? "wip" : "") });
      li.append(el("span", { className: "box", textContent: done ? "☑" : "☐" }));
      li.append(el("span", { className: "t" }, t.title || ""));
      ul.append(li);
    }
    body.append(ul);
  }

  // documents — specs (editable/frozen per state) then docs (read-only here;
  // the Docs tab is where docs are edited)
  for (const d of f.documents || []) body.append(docBlock(d, { readOnly: d.kind === "doc" }));

  // open flags = blockers
  if (f.open_flags?.length) {
    const fl = el("div", {});
    fl.append(el("label", { className: "k", textContent: "blockers (open flags)" }));
    for (const x of f.open_flags) fl.append(el("div", { className: "tag" }, `${x.display_name || ""} ${x.description || ""}`));
    body.append(fl);
  }
  c.append(body);
  return c;
}

// A document row: the primary action OPENS it rendered in md-converter (the
// markdown rides in the URL via /open → ?c=). No inline raw-markdown expand.
// Non-frozen docs get an explicit "edit" toggle; frozen ones are read-only.
function docBlock(d, { readOnly = false } = {}) {
  const wrap = el("div", { className: "docrow" });
  const label = d.kind === "doc"
    ? `Doc - ${d.title || "(untitled)"}`
    : `${d.kind} v${d.seq}${d.frozen ? " · frozen " + (d.frozen_date || "") : ""}: ${d.title || ""}`;
  const open = el("a", {
    className: "act primary", href: "/api/documents/" + d.document_id + "/open",
    target: "_blank", rel: "noopener", textContent: "open in md-converter ↗",
  });
  const head = el("div", { className: "docrow-head" }, el("span", { className: "docrow-label" }, label), open);
  wrap.append(head);

  if (readOnly) return wrap;   // open-link only — no edit toggle, no lock-note

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
let docsQuery = "";   // persists across re-renders so the search box keeps its value

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

  // search bar — filters by doc title or feature on every keystroke
  const search = el("input", { type: "text", className: "search", placeholder: "search docs…", value: docsQuery });
  const results = el("div", {});
  const draw = () => {
    const q = docsQuery.trim().toLowerCase();
    const matched = q
      ? docs.filter((d) => `${d.title || ""} ${d.feature_title || ""}`.toLowerCase().includes(q))
      : docs;
    results.replaceChildren();
    if (!matched.length) { results.append(el("div", { className: "muted" }, "No docs match.")); return; }
    const byFeat = {};
    for (const d of matched) (byFeat[d.feature_title || "— unlinked —"] ||= []).push(d);
    for (const [title, list] of Object.entries(byFeat)) {
      const c = el("div", { className: "card" });
      c.append(el("h2", {}, title));
      for (const d of list) c.append(docBlock(d));
      results.append(c);
    }
  };
  search.oninput = () => { docsQuery = search.value; draw(); };
  root.append(search, results);
  draw();
}

// ── Flags ──────────────────────────────────────────────────────────────────────
let flagFilter = "open";   // open | resolved | all — persists across re-renders

// New-flag form in a 600×400 modal — Create bottom-left, Cancel bottom-right.
function openNewFlagModal(features) {
  const name = el("input", { type: "text", placeholder: "display name (e.g. SC-001)" });
  const desc = el("textarea", { rows: 4, placeholder: "[Area] description | Blocker for: …" });
  const feat = el("select", {});
  feat.append(el("option", { value: "", textContent: "— no feature —" }));
  for (const f of features) feat.append(el("option", { value: f.feature_id, textContent: f.title }));
  const prio = el("select", {});
  for (const p of ["High", "Medium", "Low"]) prio.append(el("option", { value: p, selected: p === "Medium", textContent: p }));
  const create = el("button", { className: "act primary", type: "button", textContent: "Create" });
  const cancel = el("button", { className: "act", type: "button", textContent: "Cancel" });
  const form = el("div", { className: "modal-form" },
    el("span", { className: "k" }, "name"), name,
    el("span", { className: "k" }, "description"), desc,
    el("span", { className: "k" }, "feature"), feat,
    el("span", { className: "k" }, "priority"), prio);
  const close = openModal({ title: "New flag", bodyNode: form,
    footNodes: [create, cancel], width: 600, height: 400 });
  create.onclick = async () => {
    if (!desc.value) return toast("description required");
    create.disabled = true; create.textContent = "Creating…";
    try {
      await api("/flags", "POST", { display_name: name.value || null, description: desc.value,
        feature_id: feat.value || null, priority: prio.value });
      close(); setStatus("flag created"); load("flags");
    } catch (e) { toast("error: " + e.message); create.disabled = false; create.textContent = "Create"; }
  };
  cancel.onclick = close;
  desc.focus();
}

async function renderFlags(root) {
  const { flags, features } = await api("/flags");
  root.replaceChildren();

  // open | resolved | all toggle (segmented) + the new-flag modal trigger
  const bar = el("div", { className: "filters seg" });
  for (const [key, label] of [["open", "Open"], ["resolved", "Resolved"], ["all", "All"]]) {
    const chip = el("button", { className: "chip" + (flagFilter === key ? " on" : ""), textContent: label });
    chip.onclick = () => { flagFilter = key; renderFlags(root); };
    bar.append(chip);
  }
  const newBtn = el("button", { className: "act newflag", type: "button", textContent: "＋ New flag" });
  newBtn.onclick = () => openNewFlagModal(features);
  root.append(el("div", { className: "flagbar" }, bar, newBtn));

  // grouped by feature, filtered by the toggle
  const shown = flags.filter((f) =>
    flagFilter === "all" ? true : flagFilter === "resolved" ? f.resolved : !f.resolved);
  if (!shown.length) { root.append(el("div", { className: "muted" }, "No flags in this view.")); return; }
  const byFeat = {};
  for (const f of shown) (byFeat[f.feature_title || "— unlinked —"] ||= []).push(f);
  for (const [title, list] of Object.entries(byFeat)) {
    const c = el("div", { className: "card" });
    c.append(el("h2", {}, title));
    for (const f of list) c.append(flagRow(f));
    root.append(c);
  }
}

function flagRow(f) {
  // Expandable: collapsed row shows priority + name + description on one line;
  // expanding reveals the resolution note (resolved) or the resolve action (open).
  const row = el("details", { className: "flag" + (f.resolved ? " resolved" : "") });
  const head = el("summary", { className: "flag-head" });
  head.append(el("span", { className: "pill " + (f.priority || "").toLowerCase() }, f.priority || ""));
  const d = el("span", { className: "desc" });
  d.append(el("b", {}, (f.display_name ? f.display_name + " " : "")), esc(f.description || ""));
  head.append(d);
  row.append(head);

  const body = el("div", { className: "flag-body" });
  if (f.resolved) {
    body.append(el("div", { className: "tag" }, `resolved ${f.resolved_date || ""} — ${f.resolution_notes || ""}`));
  } else {
    const btn = el("button", { className: "act", textContent: "resolve" });
    btn.onclick = async () => {
      const notes = prompt("Resolution notes:");
      if (notes === null) return;
      try { await api("/flags/" + f.flag_id, "PATCH", { resolved: 1, resolution_notes: notes }); setStatus("flag resolved"); load("flags"); }
      catch (e) { toast("error: " + e.message); }
    };
    body.append(btn);
  }
  row.append(body);
  return row;
}

// ── Scripts ─────────────────────────────────────────────────────────────────────
async function renderScripts(root) {
  const { scripts } = await api("/scripts");
  root.replaceChildren();
  root.append(el("div", { className: "muted" },
    "Run a maintenance script. Output appears below it. Per-instance DB edits → run Snapshot, then Render flat, to persist them to git-tracked text."));

  // Windows Test VM — opt-in, link-only. Links this fork to an operator-run
  // Windows VM for installer/system-level testing. Config lives in instance.json
  // (no secrets — a key PATH only); every field is live-tested before save.
  const vmc = el("div", { className: "card" });
  vmc.append(el("h2", {}, "Windows Test VM",
    el("span", { className: "pill", textContent: " opt-in" })));
  vmc.append(el("div", { className: "muted" },
    "Link this fork to a Windows VM you already run, for high-fidelity installer/system-level testing. " +
    "Link-only — the VM (OpenSSH, a clean snapshot, the transfer dir, the toolchain via the admin configure_winbox skill) is yours to set up. " +
    "Every field is validated live before it saves."));
  const vmbtn = el("button", { className: "act primary", textContent: "configure…" });
  vmbtn.onclick = openWinVmModal;
  vmc.append(vmbtn);
  root.append(vmc);

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

// Windows Test VM wizard — a single link-only modal (the house openModal/el
// pattern). The fields map 1:1 to the instance.json `vm` block; the five checks
// each hit POST /api/vm/validate/{check} with the IN-PROGRESS form, so the
// operator tests before saving. No secrets here — ssh_key_path is a PATH.
const VM_FIELDS = [
  ["domain", "win-test", "libvirt domain name (virsh target)"],
  ["ssh_host", "127.0.0.1", "guest OpenSSH host"],
  ["ssh_port", "22", "guest OpenSSH port"],
  ["ssh_user", "tester", "guest SSH user"],
  ["ssh_key_path", "~/.ssh/sc_win_test", "PATH to the private key — never the key itself"],
  ["transfer_dir", "/var/sc/win-xfer", "host-side dir the guest sees (virtio-fs share / scp target)"],
  ["snapshot", "clean", "named clean snapshot to revert to between runs"],
  ["libvirt_uri", "qemu:///system", "OPTIONAL — virsh connection; set for a system-scope domain (default: qemu:///session)"],
];
const VM_CHECKS = [
  ["domain", "VM exists + visible to libvirt"],
  ["ssh", "SSH auth + remote exec work"],
  ["transfer", "artifact transfer dir reachable"],
  ["snapshot", "named clean snapshot exists"],
  ["toolchain", "box is provisioned (configure_winbox ran)"],
];

async function openWinVmModal() {
  let saved = {};
  try { saved = (await api("/vm")).vm || {}; } catch { /* none yet */ }

  const inputs = {};
  const form = el("div", { className: "modal-form" });
  for (const [key, ph, hint] of VM_FIELDS) {
    const inp = el("input", { type: "text", placeholder: ph, value: saved[key] ?? "", title: hint });
    inputs[key] = inp;
    form.append(el("span", { className: "k", title: hint }, key), inp);
  }

  const collect = () => {
    const vm = {};
    for (const [key] of VM_FIELDS) {
      let v = inputs[key].value.trim();
      if (key === "ssh_port") v = Number(v) || 22;
      if (v !== "") vm[key] = v;
    }
    return vm;
  };

  // results panel: one row per check (✓/✗ + output), like the Scripts run block
  const results = el("div", {});
  const note = el("div", { className: "muted" },
    "Your VM must already have OpenSSH, a clean snapshot, the transfer dir, and the toolchain " +
    "(admin's configure_winbox). The wizard validates the link — it does not set the VM up.");

  const runAll = el("button", { className: "act", textContent: "run all checks" });
  runAll.onclick = async () => {
    const vm = collect();
    results.replaceChildren();
    runAll.disabled = true;
    for (const [check, label] of VM_CHECKS) {
      const row = el("div", { className: "card" });
      const head = el("div", {},
        el("span", { className: "pill", textContent: "…" }),
        el("span", {}, "  " + check + " — " + label));
      const out = el("pre", { className: "doc-body", hidden: true });
      row.append(head, out);
      results.append(row);
      try {
        const r = await fetch("/api/vm/validate/" + check,
          { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ vm }) });
        const data = await r.json();
        const pill = head.firstChild;
        pill.textContent = data.ok ? "✓" : "✗";
        pill.className = "pill " + (data.ok ? "ok" : "warn");
        out.hidden = false; out.textContent = data.output || "(no output)";
      } catch (e) {
        const pill = head.firstChild;
        pill.textContent = "✗"; pill.className = "pill warn";
        out.hidden = false; out.textContent = "error: " + e.message;
      }
    }
    runAll.disabled = false;
  };

  const save = el("button", { className: "act primary", textContent: "save" });
  const cancel = el("button", { className: "act", textContent: "close" });
  const close = openModal({
    title: "Windows Test VM", width: 680, height: 760,
    bodyNode: el("div", {}, form, el("div", { className: "modal-form-foot" }, runAll), note, results),
    footNodes: [save, cancel],
  });
  save.onclick = async () => {
    save.disabled = true; setStatus("saving VM config…");
    try {
      await api("/vm", "PUT", { vm: collect() });
      setStatus("VM config saved");
      close();
    } catch (e) { toast("error: " + e.message); save.disabled = false; }
  };
  cancel.onclick = close;
}

// ── Map (dr_* repo catalogue) ───────────────────────────────────────────────────
function bars(items, label, val) {
  const max = Math.max(1, ...items.map(val));
  const wrap = el("div", { className: "bars" });
  for (const it of items) {
    const row = el("div", { className: "bar-row" });
    row.append(el("span", { className: "bar-label" }, label(it)));
    const track = el("div", { className: "bar-track" });
    const fill = el("div", { className: "bar-fill" });
    fill.style.width = Math.round((val(it) / max) * 100) + "%";
    track.append(fill);
    row.append(track, el("span", { className: "bar-n" }, String(val(it))));
    wrap.append(row);
  }
  return wrap;
}

async function renderMap(root) {
  const m = await api("/map");
  root.replaceChildren();
  if (!m.repo) {
    root.append(el("div", { className: "card muted" },
      "Repo not mapped yet. Run Map (Scripts tab) or `make map` to scan the repo into the dr_* catalogue."));
    return;
  }
  const r = m.repo;
  const head = el("div", { className: "card" });
  head.append(el("h2", {}, r.name || "(repo)"));
  head.append(el("div", { className: "grid2" },
    el("span", { className: "k" }, "branch"), el("span", {}, r.default_branch || "—"),
    el("span", { className: "k" }, "remote"), el("span", { className: "muted" }, r.remote || "—"),
    el("span", { className: "k" }, "files"), el("span", {}, String(m.total_files)),
    el("span", { className: "k" }, "mapped"), el("span", { className: "muted" }, r.mapped_at || "—")));
  const remap = el("button", { className: "act", textContent: "re-map ↻" });
  remap.onclick = async () => {
    remap.disabled = true; setStatus("mapping…");
    try { await fetch("/api/scripts/map", { method: "POST" }); setStatus("mapped"); renderMap(root); }
    finally { remap.disabled = false; }
  };
  head.append(remap);
  root.append(head);

  if (m.by_lang.length) {
    const c = el("div", { className: "card" });
    c.append(el("h2", {}, "Languages"));
    c.append(bars(m.by_lang, (x) => x.lang, (x) => x.n));
    root.append(c);
  }
  if (m.by_role.length) {
    const c = el("div", { className: "card" });
    c.append(el("h2", {}, "File roles"));
    c.append(bars(m.by_role, (x) => x.role, (x) => x.n));
    root.append(c);
  }
  if (m.deps.length) {
    const c = el("div", { className: "card" });
    c.append(el("h2", {}, `Dependencies (${m.deps.length})`));
    for (const d of m.deps) c.append(el("div", { className: "tag" },
      `${d.manager} · ${d.name} ${d.version || ""}${d.kind === "dev" ? " (dev)" : ""}`));
    root.append(c);
  }
  if (m.env.length) {
    const c = el("div", { className: "card" });
    c.append(el("h2", {}, `Env vars (${m.env.length})`));
    for (const e of m.env) c.append(el("div", { className: "tag" }, `${e.name}  — ${e.source_file}`));
    root.append(c);
  }
}

// ── Worktrees (git hygiene) ──────────────────────────────────────────────────
// Live, report-only view of the repo: which worktrees are dirty (yellow/orange),
// which local branches are stale (PR merged → prunable), what's clean. The
// server computes it on demand from disk in one pass — no shell is ever polled.
// The refresh button is the ONLY trigger; it does the network fetch (origin +
// `gh`) for fresh behind-counts and PR state. Nothing here mutates the repo.
async function renderWorktrees(root, opts = {}) {
  if (!opts.fetch) root.replaceChildren(el("div", { className: "card muted" }, "Reading repo state…"));
  let d;
  try { d = await api("/git-state?fetch=" + (opts.fetch ? "1" : "0")); }
  catch (e) { root.replaceChildren(el("div", { className: "card" }, "error: " + e.message)); return; }
  root.replaceChildren();

  // header: repo + summary pills + provenance + the one trigger (refresh)
  const s = d.summary;
  const head = el("div", { className: "card" });
  head.append(el("h2", {}, d.repo.name));
  head.append(el("div", { className: "wt-summary" },
    el("span", { className: "pill" + (s.dirty_worktrees ? " warn" : " ok") }, `${s.dirty_worktrees} dirty`),
    el("span", { className: "pill" }, `${s.stale_branches} stale`),
    el("span", { className: "pill" + (s.all_clean ? " ok" : "") },
      s.all_clean ? "all clean" : `${s.worktrees} worktree${s.worktrees !== 1 ? "s" : ""}`)));
  head.append(el("div", { className: "muted wt-prov" },
    [`default: ${d.repo.default_branch}`,
     `fetch: ${d.fetched ? "fresh" : "skipped — click refresh"}`,
     `gh: ${d.gh_available ? "ok" : "unavailable — merge state best-effort"}`].join("  ·  ")));
  const refresh = el("button", { className: "act", textContent: "refresh ↻" });
  refresh.title = "re-scan worktrees + fetch origin & gh for fresh behind-counts and PR state";
  refresh.onclick = async () => {
    refresh.disabled = true; setStatus("scanning…");
    try { await renderWorktrees(root, { fetch: true }); setStatus("scanned"); }
    catch { setStatus("scan failed"); }
  };
  head.append(refresh);
  root.append(head);

  // worktrees — dot is green (clean) or yellow/orange (dirty)
  const wc = el("div", { className: "card" });
  wc.append(el("h2", {}, "Worktrees"));
  for (const w of d.worktrees) {
    const dirty = w.dirty > 0;
    const main = el("div", { className: "wt-main" });
    const top = el("div", { className: "wt-top" });
    top.append(el("span", { className: "wt-path" }, w.path === "." ? ".  (main)" : w.path));
    top.append(el("span", { className: "mono wt-branch" }, w.branch || "(detached)"));
    main.append(top);
    const bits = [dirty ? `✎ ${w.dirty} uncommitted` : "clean"];
    if (w.behind) bits.push(`${w.behind} behind`);
    if (w.ahead) bits.push(`${w.ahead} ahead`);
    main.append(el("div", { className: "wt-meta muted" }, bits.join("  ·  ")));
    if (dirty && w.dirty_files.length) {
      const det = el("details", { className: "wt-files" });
      det.append(el("summary", {}, `${w.dirty} changed file${w.dirty !== 1 ? "s" : ""}`));
      const extra = w.dirty > w.dirty_files.length ? `\n… +${w.dirty - w.dirty_files.length} more` : "";
      det.append(el("pre", {}, w.dirty_files.join("\n") + extra));
      main.append(det);
    }
    wc.append(el("div", { className: "wt-row" },
      el("span", { className: "wt-dot " + (dirty ? "dirty" : "clean") }), main));
  }
  root.append(wc);

  // stale branches — report only, copy-paste prune command, never auto-deleted
  const stale = d.branches.filter((b) => b.stale);
  const sc = el("div", { className: "card" });
  sc.append(el("h2", {}, `Stale branches — ${stale.length}`));
  sc.append(el("div", { className: "muted wt-prov" },
    "Local branches whose PR is merged. Reported only — copy a command to prune. Nothing is deleted for you."));
  if (!stale.length) sc.append(el("div", { className: "muted" }, "None — no merged branches lingering."));
  for (const b of stale) {
    const main = el("div", { className: "wt-main" });
    const top = el("div", { className: "wt-top" });
    top.append(el("span", { className: "mono wt-branch" }, b.name));
    if (b.pr) top.append(el("span", { className: "pill" }, "PR #" + b.pr.number));
    main.append(top);
    main.append(el("code", { className: "wt-cmd" }, "git branch -D " + b.name));
    sc.append(el("div", { className: "wt-row" },
      el("span", { className: "wt-dot stale" }), main));
  }
  const unknown = d.branches.filter((b) => b.merged === null);
  if (unknown.length) sc.append(el("div", { className: "muted wt-prov" },
    `${unknown.length} branch(es) with unknown merge state (gh unavailable): ${unknown.map((b) => b.name).join(", ")}`));
  root.append(sc);
}

// ── Tabs + boot ────────────────────────────────────────────────────────────────
const VIEWS = {
  shells: ["#view-shells", renderShells],
  skills: ["#view-skills", renderSkills],
  roadmap: ["#view-roadmap", renderRoadmap],
  docs: ["#view-docs", renderDocs],
  flags: ["#view-flags", renderFlags],
  worktrees: ["#view-worktrees", renderWorktrees],
  map: ["#view-map", renderMap],
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
// Hash routing: the active tab lives in the URL (#roadmap), so a refresh stays
// put (and re-fetches that tab) instead of snapping back to Shells. Tabs set the
// hash; hashchange drives show — back/forward and deep links work too. The
// roadmap tab carries its sub-view in the hash: #roadmap (board) | #roadmap-flow.
function routeFromHash() {
  const raw = location.hash.slice(1);
  if (raw === "roadmap" || raw.startsWith("roadmap-")) {
    roadmapView = raw === "roadmap-flow" ? "flow" : "board";
    show("roadmap");
    return;
  }
  show(VIEWS[raw] ? raw : "shells");
}
document.querySelectorAll("nav button").forEach((b) => (b.onclick = () => { location.hash = b.dataset.tab; }));
window.addEventListener("hashchange", routeFromHash);
// Close any open popover menu on an outside click (one handler for all .gmenu).
document.addEventListener("mousedown", (e) => {
  for (const m of document.querySelectorAll(".gmenu:not([hidden])"))
    if (!m.parentElement.contains(e.target)) m.hidden = true;
});
// Esc dismisses the topmost modal.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const overlays = document.querySelectorAll(".modal-overlay");
    overlays[overlays.length - 1]?.remove();
  }
});
$("#snapshot").onclick = async () => {
  setStatus("snapshotting…");
  try { const r = await api("/snapshot", "POST"); toast(r.output || "done"); setStatus("snapshot done"); }
  catch (e) { toast("error: " + e.message); }
};
$("#publish").onclick = async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  setStatus("publishing…");
  try {
    const r = await api("/publish", "POST");
    toast(r.output || "published");
    setStatus(r.pr_url ? "published → PR ready" : "published");
    if (r.pr_url) window.open(r.pr_url, "_blank", "noopener");
  } catch (e) { toast("publish error: " + e.message); setStatus("publish failed"); }
  finally { btn.disabled = false; }
};
(async () => {
  try { const h = await api("/health"); $("#repo").textContent = h.repo; setStatus("port " + h.port); }
  catch { setStatus("offline"); }
  routeFromHash();   // honor #tab on load (refresh / deep link), else Shells
})();
