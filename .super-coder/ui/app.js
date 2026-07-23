// super-coder review UI — vanilla JS, no build step. Talks to the same-origin
// stdlib API. Read everything; edit only what the laws and freeze rules allow.

const $ = (s, r = document) => r.querySelector(s);
const el = (t, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(t), props);
  for (const k of kids) n.append(k?.nodeType ? k : document.createTextNode(k ?? ""));
  return n;
};
const esc = (s) => (s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// Unified list search box — identical look + placement (first element under the
// header) on every page that filters a list (Roadmap board, Docs, Flags).
// `onq(value)` fires on each keystroke; the caller owns the persisted query
// string so the box keeps its value across re-renders.
function searchBar(placeholder, value, onq) {
  const input = el("input", { type: "text", className: "search", placeholder, value });
  input.oninput = () => onq(input.value);
  return input;
}

// Feature-less items group under this label on Docs and Flags; it always sorts
// to the BOTTOM of the grouped list (linked groups first, in their natural
// order). Array.sort is stable, so non-unlinked groups keep their order.
const UNLINKED = "— unlinked —";
const unlinkedLast = (entries) =>
  entries.sort((a, b) => (a[0] === UNLINKED ? 1 : 0) - (b[0] === UNLINKED ? 1 : 0));

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
  // publish/scripts report failure as {ok:false, output:<step trace>} with no
  // `error` key — the trace names the refusing guard and the remedy, so it is
  // the message, not statusText.
  if (!r.ok) throw new Error(data.error || data.output || r.statusText);
  return data;
}

function toast(msg) {
  const t = el("div", { className: "toast" }, msg);
  document.body.append(t);
  // Multi-line traces (publish refusals) need longer than one-liners.
  setTimeout(() => t.remove(), Math.min(12000, Math.max(4000, String(msg).length * 30)));
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
let shellTab = "harness";     // 'harness' | 'skills' | 'models'
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

  // rename shell — fix a display_name that got wonked at creation
  const renBtn = el("button", { className: "act", type: "button", textContent: "✎ Rename" });
  renBtn.onclick = async () => {
    const name = (prompt("New display name", s.display_name) || "").trim();
    if (!name || name === s.display_name) return;
    try {
      await api("/shells/" + selectedShell, "PATCH", { display_name: name });
      setStatus("shell renamed — " + name); renderShells(root);
    } catch (e) { toast("error: " + e.message); }
  };
  sub.append(renBtn);

  // delete shell — soft-delete the selected shell, then re-render
  const delBtn = el("button", { className: "act", type: "button", textContent: "✕ Delete shell" });
  delBtn.onclick = async () => {
    if (!confirm("Delete shell “" + s.display_name + "”?")) return;
    await api("/shells/" + selectedShell, "DELETE");
    selectedShell = null;
    renderShells(root);
  };
  sub.append(delBtn);
  // Default Models is fork-global config — the shell-scoped header (picker,
  // role/mandate, ＋New shell) is greyed out and inert there, not load-bearing.
  if (shellTab === "models") sub.classList.add("subbar-inert");
  root.append(sub);

  // sub-tabs — Harness / Skills scoped to the selected shell; Default Models
  // is the fork-global launch matrix (same content from any shell)
  const tabs = el("div", { className: "vtabs" });
  for (const [key, label] of [["harness", "Harness"], ["skills", "Skills"],
                              ["models", "Default Models"]]) {
    const b = el("button", { className: shellTab === key ? "active-tab" : "", type: "button", textContent: label });
    b.onclick = () => { shellTab = key; renderShells(root); };
    tabs.append(b);
  }
  root.append(tabs);

  const pane = el("div", { className: "shell-pane" });
  root.append(pane);
  if (shellTab === "harness") renderHarness(pane, s);
  else if (shellTab === "models") renderDefaultModels(pane, s);
  else renderSkillViewer(pane, s);
}

// Default Models — the flavor_defaults launch matrix: per flavor, a model per
// harness and ONE starred default harness (the two launch defaults run.py
// resolves at boot). Fork-global config — the selected shell's flavor leads,
// but the matrix is the same from any shell.
//
// The model picker is family-first: family chips are FILTERS — click one to
// narrow the list to that line (opus / sonnet / fable, deepseek / glm, …),
// then select a model from its stacked cards. Where a family has a
// self-tracking CLI alias (opus/sonnet/haiku) an "alias — tracks latest" card
// is pinned on top of that family's list; elsewhere the newest release
// already leads. The panel opens on focus (family chips first), the card
// list appears once a family is active or a query is typed, "all" + Enter
// browses everything, Enter stores any typed id as-is, Escape / outside
// click / a pick collapses. Catalog: /api/models v2 — advisory, never a
// constraint. Payload v3 adds local route authority + effort capability: a
// public suggestion remains typeable, but only a locally available model with
// verified high effort is sprint-runnable through `sc models resolve`.
const DM_MODEL_CAP = 60;   // rendered cards per view — "all" on opencode is huge

function dmModelPicker(harness, cat, row, save) {
  const data = cat.harnesses?.[harness] || { families: [], models: [] };
  const currentRoute = (data.models || []).find((m) => m.id === row.model);
  const current = el("span", { className: "dm-current" + (row.model ? "" : " dm-unset"),
                               textContent: row.model || "harness default",
                               title: currentRoute
                                 ? `${currentRoute.availability || "advisory"} · ${currentRoute.source || "unknown source"}`
                                 : (row.model ? "not present in the latest catalogue" : "") });
  const input = el("input", { className: "dm-search",
                              placeholder: "search · click a family to filter · “all” ⏎" });
  const results = el("div", { className: "dm-results", hidden: true });
  let open = false, activeFam = null, allMode = false;

  // model family labels are raw ("claude-opus"); family chips are stripped
  const famOf = (m) => harness === "claude"
    ? (m.family || "").replace(/^claude-/, "") : (m.family || "");

  const close = () => { open = false; activeFam = null; allMode = false; input.value = ""; paint(); };
  const pick = async (value) => {
    await save(value);
    current.textContent = value || "harness default";
    current.classList.toggle("dm-unset", !value);
    close();
  };

  const famChip = (f) => {
    const c = el("button", { className: "dm-chip dm-fam" + (activeFam === f.family ? " dm-active" : ""),
                             type: "button",
                             title: `filter to the ${f.family} line (${f.n} model${f.n > 1 ? "s" : ""})` });
    c.append(el("b", {}, f.family), el("span", { className: "dm-chip-sub" }, String(f.n)));
    c.onclick = () => { activeFam = activeFam === f.family ? null : f.family; allMode = false; paint(); };
    return c;
  };
  const mcard = (id, sub, cls) => {
    const c = el("button", { className: "dm-mcard" + (cls ? " " + cls : ""), type: "button", title: id });
    c.append(el("b", {}, id), el("span", { className: "dm-mcard-sub" }, sub || ""));
    c.onclick = () => pick(id);
    return c;
  };
  const routeSub = (m) => {
    const efforts = m.supported_efforts || [];
    const route = m.availability === "available"
      ? (efforts.includes("high") ? "local · high-effort route" : "local · no verified high effort")
      : (m.availability || "advisory");
    return [route, m.source, m.release_date].filter(Boolean).join(" · ");
  };

  const paint = () => {
    results.textContent = "";
    if (!open) { results.hidden = true; return; }
    const q = input.value.trim().toLowerCase();
    const hit = (s) => !q || (s || "").toLowerCase().includes(q);

    // family filter row — always visible while open; the query narrows it too
    const fams = (data.families || []).filter((f) => hit(f.family) || f.family === activeFam);
    if (fams.length) {
      results.append(el("div", { className: "dm-sect" }, "families — click to filter"));
      const frow = el("div", { className: "dm-famrow" });
      for (const f of fams) frow.append(famChip(f));
      results.append(frow);
    }

    // stacked model cards — once a family is active, a query is typed, or all-mode
    let models = null;
    if (activeFam) {
      models = (data.models || []).filter(
        (m) => famOf(m) === activeFam && (hit(m.id) || hit(m.name)));
    } else if (allMode) {
      models = data.models || [];
    } else if (q) {
      models = (data.models || []).filter(
        (m) => hit(m.id) || hit(m.name) || hit(m.family));
    }
    if (models) {
      const label = activeFam ? `${activeFam} models` : "models";
      results.append(el("div", { className: "dm-sect" },
        `${label} (${models.length})`
        + (models.length > DM_MODEL_CAP ? ` — first ${DM_MODEL_CAP}, type to narrow` : "")));
      const list = el("div", { className: "dm-cardlist" });
      // aliased family → pin the self-tracking card on top of its list
      const famMeta = activeFam && (data.families || []).find((f) => f.family === activeFam);
      if (famMeta && !models.some((m) => m.id === famMeta.latest) && hit(famMeta.latest))
        list.append(mcard(famMeta.latest, "alias — tracks this family's latest", "dm-alias"));
      for (const m of models.slice(0, DM_MODEL_CAP))
        list.append(mcard(m.id, routeSub(m)));
      if (q && !allMode)
        list.append(mcard(input.value.trim(), "use as typed", "dm-raw"));
      results.append(list);
      if (!models.length && !q)
        results.append(el("div", { className: "dm-sect" }, "no models in this family"));
    } else {
      results.append(el("div", { className: "dm-sect" },
        "click a family to filter · type to search · “all” ⏎ for everything"));
    }
    results.hidden = false;
  };

  input.onfocus = () => { if (!open) { open = true; paint(); } };
  input.oninput = () => { allMode = false; paint(); };
  input.onkeydown = (e) => {
    if (e.key === "Escape") { close(); input.blur(); return; }
    if (e.key !== "Enter") return;
    const q = input.value.trim();
    if (!q) return;
    if (q.toLowerCase() === "all") { allMode = true; activeFam = null; input.value = ""; paint(); }
    else pick(q);
  };
  // outside click collapses; chips/cards live inside `results`, so picks land
  // first. Self-unregisters once this render generation is detached.
  const outside = (e) => {
    if (!results.isConnected) { document.removeEventListener("mousedown", outside); return; }
    if (open && e.target !== input && !results.contains(e.target)) close();
  };
  document.addEventListener("mousedown", outside);
  return { current, input, results };
}

async function renderDefaultModels(root, s) {
  root.textContent = "";
  let fd;
  try { fd = await api("/flavor-defaults"); }
  catch (e) { root.append(el("div", { className: "vpanel" }, "flavor-defaults error: " + e.message)); return; }
  let cat = { harnesses: {}, sources: [], fetched_at: null, stale: true };
  try { cat = await api("/models"); } catch { /* pickers still store typed ids */ }

  const head = el("div", { className: "viewer-head" }, microlabel("Default Models"));
  const refresh = el("button", { className: "act", type: "button", textContent: "↻ Refresh models" });
  refresh.onclick = async () => {
    refresh.disabled = true;
    setStatus("refreshing model catalog…");
    try { await api("/models?refresh=1"); setStatus("model catalog refreshed"); }
    catch (e) { toast("catalog refresh error: " + e.message); setStatus("catalog refresh failed"); }
    renderDefaultModels(root, s);
  };
  head.append(refresh);
  root.append(head);
  const when = cat.fetched_at ? new Date(cat.fetched_at).toLocaleString() : "never";
  root.append(el("div", { className: "dm-meta" },
    `catalog: ${(cat.sources || []).join(" + ") || "none"} · as of ${when}`
    + (cat.stale ? " (stale — live refresh failed)" : "")));

  // App-wide config: flavors in a stable alphabetical order (no shell-scoped
  // emphasis — the shell header above is inert on this tab), one card per
  // flavor with docs-style separation between cards.
  const flavors = Object.keys(fd.flavors).sort();
  for (const flavor of flavors) {
    const byHarness = Object.fromEntries((fd.flavors[flavor] || []).map((r) => [r.harness, r]));
    const panel = el("div", { className: "vpanel dm-card" });
    panel.append(el("div", { className: "acc-group" }, flavor));
    for (const h of fd.harnesses) {
      const row = byHarness[h] || { model: null, is_default: false };
      const star = el("input", { type: "radio", name: "dm-star-" + flavor,
                                 title: "star = default harness at launch" });
      star.checked = row.is_default;
      star.onchange = async () => {
        try {
          await api("/flavor-defaults", "POST", { flavor, harness: h, is_default: true });
          toast(`default harness: ${flavor} → ${h}`);
        } catch (e) { toast("error: " + e.message); }
        renderDefaultModels(root, s);   // reflect the sibling un-star
      };
      const picker = dmModelPicker(h, cat, row, async (value) => {
        try {
          await api("/flavor-defaults", "POST", { flavor, harness: h, model: value });
          toast(`${flavor} · ${h} → ${value || "(harness default)"}`);
        } catch (e) { toast("error: " + e.message); }
      });
      panel.append(el("div", { className: "dm-row" },
        star, el("span", { className: "dm-harness" }, h),
        picker.current, picker.input));
      panel.append(picker.results);   // full-width, collapsed until typed into
    }
    root.append(panel);
  }
  root.append(el("div", { className: "dm-note" },
    "★ = default harness at launch. Local + high-effort = sprint-runnable. Advisory entries remain free-text suggestions. Family chip = track that line's latest."));
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
// Board order: the committed funnel (in_progress → long_term), then brainstorm/
// retired, with delivered (shipped) parked at the bottom of the list.
const STATUSES = ["in_progress", "next", "near_term", "long_term", "brainstorm", "retired", "shipped"];
const SLABEL = { brainstorm: "Brainstorm", in_progress: "In Progress", next: "Next", near_term: "Near Term", long_term: "Long Term", shipped: "Shipped", retired: "Retired" };
// The five stages that sequence (carry dependency edges). brainstorm/retired are
// excluded from the Flow graph and the blocker editor — they don't relate yet.
const FLOW_STAGES = ["in_progress", "next", "near_term", "long_term", "shipped"];
let roadmapFilter = null;            // null = show all (default); single-select
let roadmapView = "board";           // "board" | "flow"
let roadmapQuery = "";               // board search; persists across re-renders
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

  // Search rides first under the header — but only on the Board sub-view. Flow
  // is a dependency graph, not a list to scan, so it carries no search box.
  // drawBoard (defined below, hoisted) repaints just the results on keystroke so
  // the box keeps focus.
  if (roadmapView === "board") {
    root.append(searchBar("search features…", roadmapQuery, (v) => { roadmapQuery = v; drawBoard(); }));
  }

  // Board ⇄ Flow segmented toggle. The sub-view rides in the URL hash (#roadmap =
  // board, #roadmap-flow = flow) so it's deep-linkable and refresh-stable;
  // routeFromHash sets roadmapView and re-renders.
  const toggle = el("div", { className: "filters centered seg view-toggle" });
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

  // Results repaint in place: filter to the single selected status, then narrow
  // by the search query (feature title / work-stream). The Board is a
  // workload-per-horizon view (status sections); work-stream grouping lives in
  // the Flow view, not here.
  const results = el("div", {});
  root.append(results);
  function drawBoard() {
    const q = roadmapQuery.trim().toLowerCase();
    const byStatus = roadmapFilter ? buckets.filter((b) => b.status === roadmapFilter) : buckets;
    const shown = q
      ? byStatus
          .map((b) => ({ ...b, features: b.features.filter((f) =>
            `${f.title || ""} #${f.feature_id} ${f.project_title || ""}`.toLowerCase().includes(q)) }))
          .filter((b) => b.features.length)
      : byStatus;
    results.replaceChildren();
    if (!shown.length) {
      results.append(el("div", { className: "muted" },
        q ? "No features match." : "No features in the selected stage."));
      return;
    }
    for (const b of shown) {
      const sec = el("div", { className: "bucket" + (roadmapCollapsed.has(b.status) ? " collapsed" : "") });
      const h = el("h2", {}, b.label, el("span", { className: "count" }, String(b.features.length)));
      h.onclick = () => {
        roadmapCollapsed.has(b.status) ? roadmapCollapsed.delete(b.status) : roadmapCollapsed.add(b.status);
        drawBoard();
      };
      sec.append(h);
      for (const f of b.features) sec.append(featureCard(f, candidates, projects));
      results.append(sec);
    }
  }
  drawBoard();
}

// Flow view: one section per work-stream (project). Inside a section the
// work-stream's features lay out left→right by planning stage (the sequence),
// and an SVG overlay wires dependencies (prerequisite → dependent). Pure DOM +
// measured coordinates — no diagram library. Work-streams are the user's
// "feature" (e.g. "Meeting Intelligence" = the mi-capture project); unassigned
// features collect in a trailing "Ungrouped" section. Shipped features are
// excluded from the Flow view entirely — it's for what's still in play; shipped
// work lives on in Board view (and stays depend-on-able in its blocker picker).
const SVGNS = "http://www.w3.org/2000/svg";
async function renderRoadmapFlow(root, buckets, projects = []) {
  const stageOf = {};
  for (const b of buckets) if (FLOW_STAGES.includes(b.status))
    for (const f of b.features) stageOf[f.feature_id] = b.status;
  // Full sequencing pool including shipped — shipped renders as a wire-free
  // "done" list in the leftmost column of each work-stream (see buildFlowGraph).
  const feats = flowCandidates(buckets);
  // The blocker/depends-on picker in the modal still spans every sequencing
  // feature, shipped included.
  const candidates = feats;
  if (!feats.length) {
    root.append(el("div", { className: "muted" }, "No features in the sequencing stages yet."));
    return;
  }


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
    if (grp.features.every((f) => stageOf[f.feature_id] === "shipped")) continue;
    const title = key === null ? "Ungrouped" : (grp.title || ("project #" + key));
    const section = el("div", { className: "flow-stream" });
    section.append(el("h2", { className: "flow-stream-head" }, title));
    const { wrap, edges } = buildFlowGraph(grp.features, stageOf, candidates, projects);
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
function buildFlowGraph(features, stageOf, candidates = [], projects = []) {
  const shownIds = new Set(features.map((f) => f.feature_id));
  const wrap = el("div", { className: "flow-wrap" });
  const inner = el("div", { className: "flow-inner" });
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("class", "flow-wires");
  const cols = el("div", { className: "flow-cols" });

  // Column order puts shipped LEFT as a wire-free "done" list; the four
  // sequencing stages follow left→right toward the horizon. (FLOW_STAGES keeps
  // shipped last for the data model; this is purely the Flow column layout.)
  const COL_ORDER = ["shipped", "in_progress", "next", "near_term", "long_term"];
  const cardOf = {};   // feature_id → card element, for wire endpoints
  for (const s of COL_ORDER) {
    const inStage = features.filter((f) => stageOf[f.feature_id] === s);
    if (!inStage.length) continue;
    const col = el("div", { className: "flow-col " + s });
    col.append(el("div", { className: "flow-col-head" }, SLABEL[s]));
    for (const f of inStage) {
      const card = el("div", { className: "flow-card " + s });
      card.dataset.fid = String(f.feature_id);
      // Shipped titles are concatenated to a reasonable length (full name in the
      // tooltip) — these cards are a compact list, not the wired sequence.
      const full = f.title || "(untitled)";
      const shown = s === "shipped" && full.length > 32 ? full.slice(0, 31).trimEnd() + "…" : full;
      card.append(el("div", { className: "flow-card-title", title: `#${f.feature_id} ${full}` }, shown,
        el("span", { className: "idnum" }, " #" + f.feature_id)));
      // Shipped cards are a title-only "done" list — no owner pill, flag count,
      // or doc links. The other stages carry the full meta + doc rows.
      if (s !== "shipped") {
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
            title: `#${d.document_id}${d.title ? " " + d.title : ""}`,
            textContent: (d.kind === "doc" ? "doc" : `${d.kind} v${d.seq}`) + " ↗" }));
          card.append(dl);
        }
      }
      // Click anywhere on the card (except a doc link) opens the edit modal.
      card.onclick = (e) => { if (e.target.closest("a")) return; openFeatureModal(f, candidates, projects); };
      col.append(card);
      cardOf[f.feature_id] = card;
    }
    cols.append(col);
  }

  inner.append(svg, cols);
  wrap.append(inner);

  // Dependency edges (prerequisite → dependent), endpoints both in this section.
  // Shipped cards are a wire-free "done" list — skip any edge that touches a
  // shipped node on EITHER end (a done prerequisite isn't worth drawing, and a
  // shipped dependent would point backward from the left-hand list).
  const edgeList = [];
  for (const f of features) for (const b of (f.blockers || []))
    if (shownIds.has(b) && stageOf[b] !== "shipped" && stageOf[f.feature_id] !== "shipped")
      edgeList.push([b, f.feature_id]);

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

// The editable form for one feature: title / status / project / summary /
// depends-on, then tasks, then specs and docs in their own sections, then open
// flags. Returns { node, save } — `node` carries no Save button (the caller
// supplies one: inline in the Board card, or the modal footer), and `save()`
// performs the PATCH + blockers PUT. Shared by the Board card's inline expand
// and the click-to-open edit modal so there is exactly one editor.
function featureForm(f, candidates = [], projects = []) {
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
    const name = (prompt("New work-stream name:") || "").trim();
    if (!name) { project.value = prevProject; return; }
    try {
      const p = await api("/projects", "POST", { title: name });
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
        textContent: `#${c.feature_id} ${c.title || "(untitled)"}` }));
    }
  }

  const gridKids = [
    el("span", { className: "k" }, "title"), title,
    el("span", { className: "k" }, "status"), status,
    el("span", { className: "k" }, "project"), project,
    el("span", { className: "k" }, "summary"), summary,
  ];
  if (blockerSelect) gridKids.push(
    el("span", { className: "k" }, "depends on"), blockerSelect);
  body.append(el("div", { className: "grid2" }, ...gridKids));

  // tasks — the spec's implementation plan, in order; done = checked + struck
  const tasks = f.tasks || [];
  const doneCount = tasks.filter((t) => t.status === "done").length;
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

  // documents — specs (editable/frozen per state) and docs (read-only here; the
  // Docs tab is where docs are edited) shown in their own labelled sections.
  const docs = f.documents || [];
  const specs = docs.filter((d) => d.kind !== "doc");
  const reads = docs.filter((d) => d.kind === "doc");
  if (specs.length) {
    const sec = el("div", { className: "doc-section" });
    sec.append(el("label", { className: "k", textContent: "specs" }));
    for (const d of specs) sec.append(docBlock(d, { readOnly: false }));
    body.append(sec);
  }
  if (reads.length) {
    const sec = el("div", { className: "doc-section" });
    sec.append(el("label", { className: "k", textContent: "docs" }));
    for (const d of reads) sec.append(docBlock(d, { readOnly: true }));
    body.append(sec);
  }

  // open flags = blockers
  if (f.open_flags?.length) {
    const fl = el("div", {});
    fl.append(el("label", { className: "k", textContent: "blockers (open flags)" }));
    for (const x of f.open_flags) fl.append(el("div", { className: "tag" }, `${x.display_name || ""} ${x.description || ""}`));
    body.append(fl);
  }

  const save = async () => {
    await api("/roadmap/" + f.feature_id, "PATCH",
              { title: title.value, roadmap_status: status.value, summary: summary.value,
                project_id: project.value && project.value !== NEW ? Number(project.value) : null });
    if (blockerSelect) {
      const ids = [...blockerSelect.selectedOptions].map((o) => Number(o.value));
      await api("/roadmap/" + f.feature_id + "/blockers", "PUT", { blocked_by: ids });
    }
  };

  return { node: body, save };
}

// Click-to-open edit modal — the same editor as the Board card's inline expand,
// reachable from any card (small Flow cards, shipped cards, and the Board card's
// ⤢ button). Save bottom-left / Cancel bottom-right; reloads the roadmap on save.
function openFeatureModal(f, candidates = [], projects = []) {
  const { node, save } = featureForm(f, candidates, projects);
  const saveBtn = el("button", { className: "act primary", type: "button", textContent: "Save" });
  const cancel = el("button", { className: "act", type: "button", textContent: "Cancel" });
  const close = openModal({
    title: (f.title || "(untitled)") + "  #" + f.feature_id,
    bodyNode: node, footNodes: [saveBtn, cancel],
    width: 680, height: 720,
  });
  saveBtn.onclick = async () => {
    saveBtn.disabled = true; saveBtn.textContent = "Saving…";
    try { await save(); close(); setStatus("feature saved"); load("roadmap"); }
    catch (e) { toast("error: " + e.message); saveBtn.disabled = false; saveBtn.textContent = "Save"; }
  };
  cancel.onclick = close;
}

function featureCard(f, candidates = [], projects = []) {
  // Expandable box: collapsed shows title + status/owner pills + a one-line
  // summary preview; expanded reveals the editable fields, docs, and blockers.
  // The ⤢ button in the head opens the same editor in a modal.
  const c = el("details", { className: "card feature" });
  // Side-bar colour: shipped specs are grey regardless of plan state. Otherwise
  // by spec-task (implementation plan) completion — all done = green, any still
  // open = sunset orange. No tasks (and not shipped) = no side bar.
  const tasks = f.tasks || [];
  const doneCount = tasks.filter((t) => t.status === "done").length;
  if (f.roadmap_status === "shipped") c.classList.add("shipped-bar");
  else if (tasks.length) c.classList.add("has-tasks", doneCount === tasks.length ? "tasks-done" : "tasks-open");
  const sum = el("summary", { className: "feature-head" });
  sum.append(el("span", { className: "feature-title" }, f.title || "(untitled)",
    el("span", { className: "idnum" }, " #" + f.feature_id)));
  const meta = el("span", { className: "feature-meta" });
  meta.append(el("span", { className: "pill " + f.roadmap_status, textContent: SLABEL[f.roadmap_status] || f.roadmap_status }));
  if (f.owner) meta.append(el("span", { className: "pill " + f.roadmap_status, textContent: f.owner }));
  if (f.open_flags?.length) meta.append(el("span", { className: "pill warn", textContent: f.open_flags.length + " ⚑" }));
  // modal trigger — preventDefault/stopPropagation so it doesn't toggle <details>
  const openBtn = el("button", { className: "act ghost feature-open", type: "button",
    title: "open in editor", textContent: "⤢" });
  openBtn.onclick = (e) => { e.preventDefault(); e.stopPropagation(); openFeatureModal(f, candidates, projects); };
  meta.append(openBtn);
  sum.append(meta);
  c.append(sum);
  if (f.summary) c.append(el("div", { className: "feature-preview muted" }, f.summary));

  const { node, save } = featureForm(f, candidates, projects);
  const saveBtn = el("button", { className: "act", textContent: "save feature" });
  saveBtn.onclick = async () => {
    try { await save(); setStatus("feature saved"); load("roadmap"); }
    catch (e) { toast("error: " + e.message); }
  };
  node.append(saveBtn);
  c.append(node);
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
  const head = el("div", { className: "docrow-head" },
    el("span", { className: "docrow-label" }, label,
      el("span", { className: "idnum" }, " #" + d.document_id)), open);
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
  if (!docs.length) {
    root.append(el("div", { className: "card muted" },
      "No docs yet. A doc is a kind='doc' document against a feature — authored by the shell, viewable here."));
    return;
  }

  // unified search bar — first under the header; filters by doc title or feature
  const search = searchBar("search docs…", docsQuery, (v) => { docsQuery = v; draw(); });
  const results = el("div", {});
  const draw = () => {
    const q = docsQuery.trim().toLowerCase();
    const matched = q
      ? docs.filter((d) =>
          `${d.title || ""} #${d.document_id} ${d.feature_title || ""} #${d.feature_id ?? ""}`
            .toLowerCase().includes(q))
      : docs;
    results.replaceChildren();
    if (!matched.length) { results.append(el("div", { className: "muted" }, "No docs match.")); return; }
    const byFeat = {};
    for (const d of matched)
      (byFeat[d.feature_title ? `${d.feature_title} #${d.feature_id}` : UNLINKED] ||= []).push(d);
    for (const [title, list] of unlinkedLast(Object.entries(byFeat))) {
      const c = el("div", { className: "card" });
      c.append(el("h2", {}, title));
      for (const d of list) c.append(docBlock(d));
      results.append(c);
    }
  };
  root.append(search, results);
  draw();
}

// ── Flags ──────────────────────────────────────────────────────────────────────
let flagFilter = "open";   // open | resolved | all — persists across re-renders
let flagQuery = "";        // flags search; persists across re-renders

// New-flag form in a 600×400 modal — Create bottom-left, Cancel bottom-right.
function openNewFlagModal(features) {
  const name = el("input", { type: "text", placeholder: "display name (e.g. SC-001)" });
  const desc = el("textarea", { rows: 4, placeholder: "[Area] description | Blocker for: …" });
  const feat = el("select", {});
  feat.append(el("option", { value: "", textContent: "— no feature —" }));
  for (const f of features) feat.append(el("option", { value: f.feature_id, textContent: `#${f.feature_id} ${f.title}` }));
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

  // unified search bar — first under the header; repaints results in place on
  // keystroke (draw, below) so the box keeps focus
  const search = searchBar("search flags…", flagQuery, (v) => { flagQuery = v; draw(); });
  root.append(search);

  // open | resolved | all segmented toggle + the new-flag modal trigger
  const bar = el("div", { className: "filters seg" });
  for (const [key, label] of [["open", "Open"], ["resolved", "Resolved"], ["all", "All"]]) {
    const chip = el("button", { className: "chip" + (flagFilter === key ? " on" : ""), textContent: label });
    chip.onclick = () => { flagFilter = key; renderFlags(root); };
    bar.append(chip);
  }
  const newBtn = el("button", { className: "act newflag", type: "button", textContent: "＋ New flag" });
  newBtn.onclick = () => openNewFlagModal(features);
  root.append(el("div", { className: "flagbar" }, bar, newBtn));

  // results repaint in place: filter by the toggle, then narrow by the query
  // (name / #id / description / feature), grouped by feature with unlinked last
  const results = el("div", {});
  root.append(results);
  function draw() {
    const q = flagQuery.trim().toLowerCase();
    const byToggle = flags.filter((f) =>
      flagFilter === "all" ? true : flagFilter === "resolved" ? f.resolved : !f.resolved);
    const shown = q
      ? byToggle.filter((f) =>
          `${f.display_name || ""} #${f.flag_id} ${f.description || ""} ${f.feature_title || ""} #${f.feature_id ?? ""}`
            .toLowerCase().includes(q))
      : byToggle;
    results.replaceChildren();
    if (!shown.length) {
      results.append(el("div", { className: "muted" }, q ? "No flags match." : "No flags in this view."));
      return;
    }
    const byFeat = {};
    for (const f of shown)
      (byFeat[f.feature_title ? `${f.feature_title} #${f.feature_id}` : UNLINKED] ||= []).push(f);
    for (const [title, list] of unlinkedLast(Object.entries(byFeat))) {
      const c = el("div", { className: "card" });
      c.append(el("h2", {}, title));
      for (const f of list) c.append(flagRow(f));
      results.append(c);
    }
  }
  draw();
}

function flagRow(f) {
  // Expandable: collapsed row shows the priority badge + title + #id;
  // expanding reveals the full description, linked items as cards, and the
  // resolution note (resolved) or the resolve action (open).
  const row = el("details", { className: "flag" + (f.resolved ? " resolved" : "") });
  const head = el("summary", { className: "flag-head" });
  const prio = f.priority || "—";
  head.append(el("span", { className: "pill prio-" + prio.toLowerCase() }, prio));
  const d = el("span", { className: "desc" });
  d.append(el("b", {}, f.display_name || "Flag"),
    el("span", { className: "flag-num" }, " #" + f.flag_id));
  head.append(d);
  row.append(head);

  const body = el("div", { className: "flag-body" });

  // Longer description, full text (no longer shown on the collapsed row).
  if (f.description) body.append(el("div", { className: "flag-desc" }, f.description));

  // Linked items as small cards. Today a flag links to at most one feature.
  const links = [];
  if (f.feature_title) links.push(["feature", f.feature_title]);
  if (links.length) {
    const lc = el("div", { className: "flag-links" });
    for (const [k, v] of links) lc.append(el("div", { className: "link-card" },
      el("span", { className: "link-k" }, k), el("span", { className: "link-v" }, v)));
    body.append(lc);
  }

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

// ── Analytics ──────────────────────────────────────────────────────────────
// Token & session analytics (doc #11). Timestamps arrive as UTC ISO; ALL
// day-grouping and displayed times are LOCAL — translated here at render,
// never on the server. The tab load runs an incremental sweep first so the
// view reflects harness data as of now.
let anFilters = { harness: "", model: "" };  // provider intentionally absent — harness + model identify the slice
let anSessions = [];      // accumulated cards across "More" pages
let anNextBefore = null;  // cursor for the next page (null = no older rows)
let anDaysLoaded = 0;     // window size loaded so far (7 per page)
let anClass = null;  // selected stat card; null = combined (all classes summed)

const AN_CLASSES = [
  ["input_tokens", "Input"], ["output_tokens", "Output"],
  ["cache_read_tokens", "Cache read"], ["cache_write_tokens", "Cache write"],
  ["reasoning_tokens", "Reasoning"],
];

const fmtTok = (n) => n == null ? "—"
  : n >= 1e9 ? (n / 1e9).toFixed(1) + "B"
  : n >= 1e6 ? (n / 1e6).toFixed(1) + "M"
  : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);
const cardTotal = (c) => ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]
  .reduce((t, k) => t + (c[k] || 0), 0);
const localDay = (iso) => iso ? new Date(iso).toLocaleDateString(undefined,
  { weekday: "short", year: "numeric", month: "short", day: "numeric" }) : "undated";
const localTime = (iso) => iso ? new Date(iso).toLocaleTimeString(undefined,
  { hour: "2-digit", minute: "2-digit" }) : "—";

function anQuery(extra = {}) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...anFilters, ...extra })) if (v) p.set(k, v);
  const s = p.toString();
  return s ? "?" + s : "";
}

const AN_RANGES = [["1W", 7], ["1M", 30], ["3M", 90], ["6M", 180]];
let anRange = 7;          // the active time chip, in days

async function anLoadPage(days) {
  const d = await api("/analytics/sessions" + anQuery({
    ...(anNextBefore ? { before: anNextBefore } : {}), days }));
  anSessions.push(...d.sessions);
  anNextBefore = d.next_before;
  anDaysLoaded += days;
}

// ── chart: local-day buckets + a monotone-cubic spline ──
// Buckets come from the loaded session cards (not a second endpoint), so the
// stat cards, the graph, and the list below always agree — same window, same
// filters, same local-day boundaries. Empty days are measured zero, not gaps.
function anBuckets(cls) {  // cls null = combined (the four classes summed)
  const days = anDaysLoaded || anRange;
  const keyOf = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  const buckets = [];
  const byKey = {};
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(Date.now() - i * 864e5);
    const b = { key: keyOf(d), date: d, value: 0 };
    buckets.push(b);
    byKey[b.key] = b;
  }
  for (const c of anSessions) {
    if (!c.started_at) continue;
    const b = byKey[keyOf(new Date(c.started_at))];
    if (b) b.value += cls ? (c[cls] || 0) : cardTotal(c);
  }
  return buckets;
}

// Monotone cubic interpolation (Fritsch–Carlson, d3 curveMonotoneX shape):
// smooth through every point with no overshoot — a spend series never dips
// below what was measured just to look curvy.
function monotonePath(pts) {
  if (pts.length < 2) return "";
  const n = pts.length;
  const dx = [], dy = [], m = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(pts[i + 1][0] - pts[i][0]);
    dy.push(pts[i + 1][1] - pts[i][1]);
    m.push(dy[i] / (dx[i] || 1));
  }
  const t = [m[0]];
  for (let i = 1; i < n - 1; i++)
    t.push(m[i - 1] * m[i] <= 0 ? 0
      : 3 * (dx[i - 1] + dx[i]) / ((2 * dx[i] + dx[i - 1]) / m[i - 1] + (dx[i] + 2 * dx[i - 1]) / m[i]));
  t.push(m[n - 2]);
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    d += `C${pts[i][0] + h},${pts[i][1] + h * t[i]} ` +
         `${pts[i + 1][0] - h},${pts[i + 1][1] - h * t[i + 1]} ` +
         `${pts[i + 1][0]},${pts[i + 1][1]}`;
  }
  return d;
}

const niceMax = (v) => {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const s of [1, 2, 5, 10]) if (v <= s * p) return s * p;
  return 10 * p;
};

function anChartLabel() {
  const cls = anClass ? AN_CLASSES.find(([k]) => k === anClass)[1] : "Total";
  const scope = [anFilters.harness || "all harnesses", anFilters.model].filter(Boolean).join(" · ");
  return `${cls} tokens — last ${anDaysLoaded || anRange} days — ${scope}`;
}

const SVG = "http://www.w3.org/2000/svg";
const svgEl = (t, attrs = {}) => {
  const n = document.createElementNS(SVG, t);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

function anChart(cls) {
  const buckets = anBuckets(cls);
  const W = 860, H = 180, L = 48, R = 14, T = 10, B = 22;
  const iw = W - L - R, ih = H - T - B;
  const ymax = niceMax(Math.max(...buckets.map((b) => b.value)));
  const x = (i) => L + (buckets.length === 1 ? iw / 2 : (i / (buckets.length - 1)) * iw);
  const y = (v) => T + ih - (v / ymax) * ih;
  const pts = buckets.map((b, i) => [x(i), y(b.value)]);

  const wrap = el("div", { className: "an-chart-wrap", tabIndex: 0 });
  wrap.append(el("div", { className: "an-chart-title" }, anChartLabel()));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "an-chart" });

  // recessive hairline grid + clean y ticks (0 / mid / max)
  for (const v of [0, ymax / 2, ymax]) {
    svg.append(svgEl("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "an-grid" }));
    const tick = svgEl("text", { x: L - 6, y: y(v) + 3, class: "an-tick", "text-anchor": "end" });
    tick.textContent = fmtTok(v);
    svg.append(tick);
  }
  // x labels: first, middle, last day (local)
  const xLabel = (i, anchor) => {
    const t2 = svgEl("text", { x: x(i), y: H - 6, class: "an-tick", "text-anchor": anchor });
    t2.textContent = buckets[i].date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return t2;
  };
  svg.append(xLabel(0, "start"), xLabel(Math.floor(buckets.length / 2), "middle"),
             xLabel(buckets.length - 1, "end"));

  const line = monotonePath(pts);
  svg.append(svgEl("path", { d: `${line}L${x(buckets.length - 1)},${y(0)}L${x(0)},${y(0)}Z`, class: "an-area" }));
  svg.append(svgEl("path", { d: line, class: "an-line" }));

  // crosshair + tooltip: aim at a day, never at the 2px line; arrows work too
  const cross = svgEl("line", { y1: T, y2: T + ih, class: "an-cross", visibility: "hidden" });
  const dot = svgEl("circle", { r: 4, class: "an-dot", visibility: "hidden" });
  svg.append(cross, dot);
  const tip = el("div", { className: "an-tip", hidden: true });
  wrap.append(svg, tip);

  let cur = -1;
  const show = (i) => {
    cur = i;
    const [px, py] = pts[i];
    cross.setAttribute("x1", px); cross.setAttribute("x2", px);
    cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", px); dot.setAttribute("cy", py);
    dot.setAttribute("visibility", "visible");
    tip.replaceChildren(
      el("b", {}, (buckets[i].value || 0).toLocaleString()),
      " ", el("span", { className: "muted" },
        buckets[i].date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })));
    tip.hidden = false;
    const frac = (px - L) / iw;
    tip.style.left = `calc(${(px / W) * 100}% - ${Math.round(frac * tip.offsetWidth)}px)`;
    tip.style.top = `${Math.max(0, (py / H) * 100 - 18)}%`;
  };
  const hide = () => { cur = -1; cross.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden"); tip.hidden = true; };
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    for (let i = 1; i < pts.length; i++) if (Math.abs(pts[i][0] - px) < Math.abs(pts[best][0] - px)) best = i;
    show(best);
  });
  svg.addEventListener("pointerleave", hide);
  wrap.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") { show(Math.min(cur + 1, pts.length - 1)); e.preventDefault(); }
    else if (e.key === "ArrowLeft") { show(Math.max(cur - 1, 0)); e.preventDefault(); }
    else if (e.key === "Escape") hide();
  });
  wrap.addEventListener("blur", hide);
  return wrap;
}

function anSelect(label, key, values, onChange) {
  const wrap = el("label", { className: "an-filter" }, microlabel(label));
  const sel = el("select", { className: "an-select" });
  sel.append(el("option", { value: "" }, "All"));
  for (const v of values) sel.append(el("option", { value: v, selected: anFilters[key] === v }, v));
  sel.onchange = () => onChange(sel.value);
  wrap.append(sel);
  return wrap;
}

function anSessionCard(c, sprintTitles) {
  const row = el("details", { className: "sess" });
  const head = el("summary", { className: "sess-head" });
  head.append(el("span", { className: "sess-time" },
    localTime(c.started_at) + "–" + localTime(c.ended_at)));
  head.append(el("span", { className: "pill" + (c.unattributed ? " warn" : "") },
    c.unattributed ? "unattributed" : c.shell || "?"));
  head.append(el("span", { className: "pill" }, c.harness));
  if (c.models) head.append(el("span", { className: "sess-model" }, c.models));
  const title = c.title || "";
  head.append(el("span", { className: "sess-title" },
    title.length > 100 ? title.slice(0, 100) + "…" : title));
  head.append(el("span", { className: "sess-tok" }, fmtTok(cardTotal(c) || null)));
  row.append(head);

  const body = el("div", { className: "sess-body" });
  if (title.length > 100) body.append(el("div", { className: "sess-full-title" }, title));
  body.append(statRow([
    ["input", fmtTok(c.input_tokens)], ["output", fmtTok(c.output_tokens)],
    ["cache read", fmtTok(c.cache_read_tokens)], ["cache write", fmtTok(c.cache_write_tokens)],
    ...(c.reasoning_tokens != null ? [["reasoning", fmtTok(c.reasoning_tokens)]] : []),
  ]));
  const meta = [];
  if (c.providers) meta.push(["provider", c.providers]);
  if (c.shell_session) meta.push(["session", c.shell_session]);
  if (c.sprint_ref) meta.push(["sprint", sprintTitles[c.sprint_ref] || "#" + c.sprint_ref]);
  if (c.status !== "ok") meta.push(["status", c.status]);
  if (meta.length) body.append(statRow(meta));
  body.append(el("code", { className: "sess-ref" }, c.harness_session_ref));
  row.append(body);
  return row;
}

async function renderAnalytics(root) {
  root.replaceChildren(el("div", { className: "muted" }, "sweeping harness data…"));
  try { await api("/analytics/sweep", "POST"); } catch { /* sweep is best-effort; show what's stored */ }
  const winDays = anDaysLoaded || anRange;
  const winFrom = new Date(Date.now() - winDays * 864e5).toISOString().slice(0, 10);
  let filters, usage;
  try {
    [filters, usage] = await Promise.all([
      api("/analytics/filters"), api("/analytics/usage?from=" + winFrom)]);
    if (!anSessions.length && !anDaysLoaded) await anLoadPage(anRange);
  } catch (e) {
    root.replaceChildren(el("div", { className: "card" }, "error: " + e.message));
    return;
  }
  root.replaceChildren();

  // filter row — harness + model scope everything below (provider is
  // deliberately absent: harness + model already identify the slice); the
  // segmented time chips sit right and set the whole window
  const reset = (k) => (v) => {
    anFilters[k] = v;
    anSessions = []; anNextBefore = null; anDaysLoaded = 0;
    renderAnalytics(root);
  };
  const rangeSeg = el("div", { className: "filters seg an-range" });
  for (const [label, days] of AN_RANGES) {
    const chip = el("button", { className: "chip" + (anRange === days && anDaysLoaded <= days ? " on" : ""),
      type: "button", textContent: label });
    chip.onclick = () => {
      anRange = days;
      anSessions = []; anNextBefore = null; anDaysLoaded = 0;
      renderAnalytics(root);
    };
    rangeSeg.append(chip);
  }
  root.append(el("div", { className: "an-filters" },
    anSelect("Harness", "harness", filters.harnesses, reset("harness")),
    anSelect("Model", "model", filters.models, reset("model")),
    rangeSeg));

  // stat cards, then the graph in ITS OWN card: no card selected = the
  // combined total is graphed; clicking a card graphs that class, clicking it
  // again deselects back to combined. Totals and buckets both come from the
  // loaded session cards, so cards, graph, and the list below always agree.
  const totals = {};
  for (const [k] of AN_CLASSES)
    totals[k] = anSessions.reduce((t, c) => c[k] == null ? t : (t ?? 0) + c[k], null);
  if (anClass && totals[anClass] == null) anClass = null;  // slice stopped exposing it
  const graphCard = el("div", { className: "card an-graph" });
  const cardRow = el("div", { className: "an-cards" });
  const drawCards = () => {
    cardRow.replaceChildren();
    for (const [k, label] of AN_CLASSES) {
      if (k === "reasoning_tokens" && totals[k] == null) continue;  // not exposed in this slice
      const c = el("button", { className: "an-card" + (anClass === k ? " on" : ""), type: "button" });
      c.append(el("span", { className: "an-card-label" }, label),
               el("span", { className: "an-card-value" }, fmtTok(totals[k] ?? 0)));
      c.onclick = () => {
        anClass = anClass === k ? null : k;
        drawCards();
        graphCard.replaceChildren(anChart(anClass));
      };
      cardRow.append(c);
    }
  };
  drawCards();
  graphCard.append(anChart(anClass));
  root.append(cardRow, graphCard);

  // usage panels — favorite model by flavor · peak day · features shipped ·
  // specs shipped · docs outstanding. Peak day is client-computed from the
  // combined buckets (all classes, all models in the slice); the shipped
  // counts are window-scoped server-side; outstanding is current-state.
  const sprintTitles = usage.sprint_titles || {};
  const panelsTop = el("div", { className: "an-panels" });
  const panels = el("div", { className: "an-panels" });
  // items: strings, or {id, label} — an id renders as #id with a copy button
  // so the number can ride straight into a Roadmap/Docs/Flags search.
  const panel = (label, valueText, items) => {
    const p = el("div", { className: "card an-panel" });
    p.append(microlabel(label), el("div", { className: "an-panel-value" }, valueText));
    for (const it of (items || []).slice(0, 5)) {
      const row = el("div", { className: "an-usage-row" });
      if (it && typeof it === "object") {
        const num = "#" + it.id;
        const btn = el("button", { className: "an-copy", type: "button", title: `copy ${num}` }, "⧉");
        btn.onclick = () => navigator.clipboard.writeText(num)
          .then(() => toast(`copied ${num}`), () => toast("copy failed"));
        row.append(el("span", { className: "an-id" }, num), btn,
                   el("span", { className: "an-row-label" }, it.label || ""));
        row.title = `${num} ${it.label || ""}`;
      } else {
        row.append(it);
      }
      p.append(row);
    }
    return p;
  };
  // row 1: favorite model by flavor + peak day. The favorite card is always
  // rendered — "—" until shell attribution has data to roll up.
  const favP = el("div", { className: "card an-panel" }, microlabel("Favorite model by flavor"));
  const favs = usage.favorite_models || [];
  if (!favs.length) favP.append(el("div", { className: "an-panel-value" }, "—"));
  for (const f of favs)
    favP.append(el("div", { className: "an-usage-row" },
      el("span", { className: "pill" }, f.flavor), " ", f.model,
      el("span", { className: "muted" }, ` — ${f.sessions} session(s)`)));
  panelsTop.append(favP);
  const peak = anBuckets(null).reduce((a, b) => (b.value > a.value ? b : a));
  panelsTop.append(panel("Peak day", peak.value ? fmtTok(peak.value) : "—",
    peak.value ? [peak.date.toLocaleDateString(undefined,
      { weekday: "short", month: "short", day: "numeric" }) + " — all models"] : []));
  // row 2: the shipped/owed trio
  panels.append(panel("Features shipped", String((usage.features_shipped || []).length),
    (usage.features_shipped || []).map((f) => ({ id: f.feature_id, label: f.title }))));
  panels.append(panel("Specs shipped", String((usage.specs_shipped || []).length),
    (usage.specs_shipped || []).map((s) => ({ id: s.document_id, label: s.title || s.feature_title }))));
  panels.append(panel("Docs outstanding", String((usage.docs_outstanding || []).length),
    (usage.docs_outstanding || []).map((f) => ({ id: f.feature_id, label: f.title }))));
  root.append(panelsTop, panels);

  // session history — grouped by LOCAL day, newest first; within a day,
  // sessions sharing a sprint_ref cluster under a sprint header with rolled-up
  // totals; solo sessions list flat.
  const list = el("div", {});
  root.append(list);
  if (!anSessions.length) {
    list.append(el("div", { className: "muted" }, "No sessions in the loaded window."));
  }
  const byDay = new Map();  // insertion order follows the DESC-sorted rows
  for (const c of anSessions) {
    const day = localDay(c.started_at);
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push(c);
  }
  for (const [day, cards] of byDay) {
    const dayCard = el("div", { className: "card an-day" });
    dayCard.append(el("h2", {}, day));
    const bySprint = new Map();
    for (const c of cards) {
      const k = c.sprint_ref || null;
      if (!bySprint.has(k)) bySprint.set(k, []);
      bySprint.get(k).push(c);
    }
    for (const [ref, group] of bySprint) {
      if (ref && group.length > 1) {
        const cl = el("div", { className: "an-sprint" });
        cl.append(el("div", { className: "an-sprint-head" },
          el("span", { className: "pill next" }, "sprint"),
          " " + (sprintTitles[ref] || "#" + ref),
          el("span", { className: "sess-tok" },
            fmtTok(group.reduce((t, c) => t + cardTotal(c), 0)))));
        for (const c of group) cl.append(anSessionCard(c, sprintTitles));
        dayCard.append(cl);
      } else {
        for (const c of group) dayCard.append(anSessionCard(c, sprintTitles));
      }
    }
    list.append(dayCard);
  }
  if (anNextBefore) {
    const more = el("button", { className: "act", type: "button", textContent: "More ↓ (7 more days)" });
    more.onclick = async () => {
      more.disabled = true;
      try { await anLoadPage(7); renderAnalytics(root); }
      catch (e) { toast("error: " + e.message); more.disabled = false; }
    };
    list.append(el("div", { className: "an-more" }, more));
  }
}

// ── Interface tab (sprint 25 seq 5) ───────────────────────────────────────────
// One interactive harness TUI per shell, brokered by the engine API inside tmux
// and streamed over WebSocket (subprotocol sc-term.v1). Left rail of shells by
// availability; available → New chat, occupied → live xterm attach (ordered,
// acked input), lost/error/unreconciled → New chat blocked. Selection lives in
// the hash (#interface/DEV3) so a refresh re-attaches the SAME session — the
// server reseeds the screen with a 0x04 full-redraw snapshot on every attach.
let ifCsrf = null;                          // browser-session CSRF token (memory only)
const ifClientId = "web-" + crypto.randomUUID();  // per-tab client identity
let ifSelected = null;                      // selected shell shortname (hash segment)
let ifAttach = null;                        // live attach record, see ifSessionPane

function ifError(r, data) {
  const e = data && data.error;
  const err = new Error(
    typeof e === "object" && e ? (e.message || e.code || r.statusText) : (e || r.statusText));
  err.status = r.status;
  err.code = typeof e === "object" && e ? e.code : undefined;
  err.body = data;
  return err;
}
// Bootstrap the operator browser session: EXCHANGES the operator capability
// (the mode-0600 token at .super-coder/run/interface/operator.token) for the
// HttpOnly SameSite=Strict cookie + the CSRF token we then send as X-CSRF on
// every call. The capability is used ONCE for the exchange, then discarded —
// never persisted (no sessionStorage) and cleared from JS memory the moment
// the session mints, so no long-lived credential sits where XSS can reach it.
// A later 401 (session gone: refresh, server restart) re-prompts the operator.
let ifOpToken = null;
// Drop any capability an older build persisted before the one-shot model.
try { sessionStorage.removeItem("sc-if-op"); } catch { /* storage blocked */ }
async function ifBootstrap() {
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() };
    if (ifOpToken) headers["Authorization"] = "Bearer " + ifOpToken;
    let r;
    try {
      r = await fetch("/api/interface/browser-sessions", {
        method: "POST", credentials: "same-origin", headers, body: "{}",
      });
    } catch (e) {
      ifOpToken = null;   // network failure — never keep a half-used capability
      throw e;
    }
    const data = await r.json().catch(() => ({}));
    if (r.ok) { ifCsrf = data.csrf; ifOpToken = null; return; }
    if (r.status === 401 && attempt === 0) {
      const t = prompt(
        "Interface operator capability required — paste the contents of\n" +
        ".super-coder/run/interface/operator.token (mode 0600, operator-only):",
        "");
      if (!t) { ifOpToken = null; throw ifError(r, data); }
      ifOpToken = t.trim();
      continue;
    }
    // EVERY other non-ok exit (rejected 401 retry, 403, 409, 422, 5xx,
    // malformed body) drops the capability uniformly — it never survives a
    // failed exchange, no matter which branch failed.
    ifOpToken = null;
    throw ifError(r, data);
  }
}
// api() twin for /api/interface/* — same-origin credentials + X-CSRF, fresh
// Idempotency-Key per ATTEMPT on POST/DELETE (an intentional retry reuses the
// caller's key). One silent re-bootstrap + retry on 401/403.
async function apiIf(path, method = "GET", body, idemKey) {
  const key = method === "GET" ? undefined : (idemKey || crypto.randomUUID());
  for (let attempt = 0; attempt < 2; attempt++) {
    if (!ifCsrf) await ifBootstrap();
    const headers = { "X-CSRF": ifCsrf };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (key) headers["Idempotency-Key"] = key;
    const r = await fetch("/api" + path, {
      method, credentials: "same-origin", headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if ((r.status === 401 || r.status === 403) && attempt === 0) { ifCsrf = null; continue; }
    const data = r.status === 204 ? {} : await r.json().catch(() => ({}));
    if (!r.ok) throw ifError(r, data);
    return data;
  }
}

// Best-effort teardown: close the stream, release the writer lease (keepalive
// so it also fires on page unload), drop the heartbeat and the terminal.
function ifDetach() {
  const a = ifAttach;
  if (!a) return;
  ifAttach = null;
  clearInterval(a.heartbeat);
  a.resizeObs?.disconnect();
  try { a.ws?.close(); } catch { /* already closed */ }
  a.term?.dispose();
  if (a.leaseId && ifCsrf)
    fetch("/api/interface/writer-leases/" + a.leaseId, {
      method: "DELETE", credentials: "same-origin", keepalive: true,
      headers: { "X-CSRF": ifCsrf, "Idempotency-Key": crypto.randomUUID() },
    }).catch(() => {});
}
window.addEventListener("pagehide", ifDetach);

const IF_BADGE = { available: "ok", starting: "warn", occupied: "accent",
  lost: "bad", error: "bad", unreconciled: "bad" };

async function renderInterface(root) {
  root.replaceChildren();
  let shells;
  try { ({ shells } = await apiIf("/interface/shells")); }
  catch (e) {
    if (e.status === 503 || e.code === "interface_unavailable") {
      ifDetach();
      root.append(el("div", { className: "card" }, "Interface unavailable on this server."));
      return;
    }
    throw e;
  }
  const rail = el("div", { className: "if-rail" });
  const pane = el("div", { className: "if-pane" });
  // Mobile shell picker (CSS-hidden on desktop): same selection, same hash.
  const picker = el("select", { className: "if-picker", title: "shell" });
  picker.append(el("option", { value: "" }, "select a shell…"));
  root.append(el("div", { className: "if-wrap" }, picker, rail, pane));
  for (const s of shells) {
    // Projection happens SERVER-side (_availability): reserved+starting →
    // starting, occupied+(idle|busy|approval|user_input) → occupied,
    // unreconciled+(lost|error) → lost|error. The rail renders it verbatim;
    // New-chat authority below stays keyed on availability === "available".
    const row = el("button", { type: "button",
      className: "if-row" + (s.shortname === ifSelected ? " active" : "") });
    const head = el("div", { className: "if-row-head" },
      el("b", {}, s.display_name || s.shortname),
      el("span", { className: "pill if-badge " + (IF_BADGE[s.availability] || "") }, s.availability));
    if (s.alerts > 0)
      head.append(el("span", { className: "pill if-badge bad",
        title: s.alerts + " unread alert(s)" }, "⚠ " + s.alerts));
    row.append(head,
      el("div", { className: "if-row-sub" }, s.shortname + (s.harness ? " · " + s.harness : "")));
    row.onclick = () => { location.hash = "interface/" + s.shortname; };
    rail.append(row);
    const opt = el("option", { value: s.shortname },
      `${s.display_name || s.shortname} · ${s.shortname} · ${s.availability}`);
    if (s.shortname === ifSelected) opt.selected = true;
    picker.append(opt);
  }
  picker.onchange = () => { if (picker.value) location.hash = "interface/" + picker.value; };
  if (!shells.length) rail.append(el("div", { className: "muted" }, "No shells."));
  const sel = shells.find((s) => s.shortname === ifSelected);
  if (!sel) {
    ifDetach();
    if (shells.length) pane.append(el("div", { className: "card muted" }, "Select a shell on the left."));
    return;
  }
  if (sel.availability === "available") return ifAvailablePane(pane, sel, root);
  if (sel.availability === "lost" || sel.availability === "error" || sel.availability === "unreconciled") {
    ifDetach();
    return ifRecoveryPane(pane, sel, root);
  }
  return ifSessionPane(pane, sel);   // occupied | starting
}

// Lost/error/unreconciled pane (spec Interface Layout): diagnostics and
// queued-work counts come straight from GET /sessions/<id> — occupancy,
// lifecycle, error_detail, the input pipeline (composer/delivery/forwarded
// seq), wake and alerts. The API exposes no explicit allowed-actions field,
// so the action gates mirror the route's own 409 preconditions exactly:
// verify any time the session isn't ended (this pane never shows ended),
// close ONLY while occupancy is unreconciled (occupied refuses with
// not_unreconciled; absence is re-proved server-side on every close). A
// closed session derives available → New chat IS the fresh-generation action.
async function ifRecoveryPane(pane, sel, root) {
  const card = el("div", { className: "card" },
    el("div", {}, el("b", {}, sel.display_name || sel.shortname), " is ",
      el("span", { className: "pill if-badge bad" }, sel.availability), "."));
  pane.append(card);
  if (!sel.session_id) {
    card.append(el("div", { className: "muted" },
      "New chat is blocked — this shell runs a legacy or unmanaged harness. " +
      "Prove the process absent (or adopt a managed generation) to free it."));
    return;
  }
  let sess;
  try { sess = await apiIf("/interface/sessions/" + sel.session_id); }
  catch (e) {
    card.append(el("div", { className: "muted" },
      "session state unavailable (" + e.message + ") — reselect the shell to retry. " +
      "Recovery actions stay hidden while the exact state is unknown."));
    return;
  }
  const diag = el("div", { className: "if-diag" });
  const drow = (k, v) => diag.append(el("span", { className: "if-stat" }, k + " ", el("b", {}, String(v ?? "—"))));
  drow("session", "#" + sess.session_id + " · gen " + (sess.generation ?? "—"));
  drow("occupancy", sess.occupancy);
  drow("lifecycle", sess.lifecycle);
  drow("harness", sess.harness || "—");
  drow("composer", sess.composer);
  drow("delivery", sess.delivery);
  drow("forwarded seq", sess.forwarded_seq);
  drow("wake", sess.wake_state);
  drow("alerts", sess.alerts ?? 0);
  card.append(diag);
  if (sess.error_detail)
    card.append(el("div", { className: "if-note" }, "diagnostics: " + sess.error_detail));
  card.append(el("div", { className: "muted" },
    "New chat is blocked until this generation is reconciled or closed."));

  const note = el("div", { className: "if-note" });
  const acts = el("div", {});
  const recon = el("button", { className: "act", type: "button", textContent: "Reconcile",
    title: "re-verify tmux/process identity — a verified unreconciled session returns to occupied" });
  recon.onclick = async () => {
    recon.disabled = true; note.textContent = "";
    try {
      const r = await apiIf("/interface/reconciliations", "POST",
        { session_id: sess.session_id, action: "verify" });
      if (r.verified) return renderInterface(root);
      note.textContent = (r.actions || [])[0] ||
        "identity could not be verified — close it if the process is gone.";
    } catch (e) { note.textContent = (e.code ? e.code + ": " : "") + e.message; }
    recon.disabled = false;
  };
  acts.append(recon);
  if (sess.occupancy === "unreconciled") {
    const close = el("button", { className: "act", type: "button", textContent: "Close session",
      title: "only after absence is proved (re-checked server-side) — frees the shell for a fresh generation" });
    close.onclick = async () => {
      if (!confirm(`Close session #${sess.session_id}? Allowed only after the process is proved absent. This frees ${sel.display_name || sel.shortname} for a new chat.`)) return;
      close.disabled = true; note.textContent = "";
      try {
        await apiIf("/interface/reconciliations", "POST",
          { session_id: sess.session_id, action: "close" });
        return renderInterface(root);
      } catch (e) {
        note.textContent = (e.code ? e.code + ": " : "") + e.message;
        close.disabled = false;
      }
    };
    acts.append(" ", close);
  }
  card.append(acts, note);
  ifSprintPanel(pane, sel);
}

// Sprint wake panel (sprint 25 seq 10): the operator-facing projection of
// the shell's planner binding — sprint doc ACTIVE/frozen, wake state, current
// batch, last outcome, park/quarantine reason — plus the open wake alerts
// (the only window into wake failures) and the retry action for parked or
// stalled work. Retry NEVER resubmits a parked batch: the server resolves it
// as audit and requeues a NEW gated batch; a parked input needs the
// operator's explicit delivered/not_delivered verdict. Read-only projections
// come from GET /interface/sprint-bindings + /interface/sprint-alerts; the
// action gates mirror the server's own preconditions (retry.applicable /
// retry.needs_outcome). Nothing renders for a shell with no sprint history.
async function ifSprintPanel(pane, sel) {
  let bindings = [], alerts = [];
  try {
    const [b, a] = await Promise.all([
      apiIf("/interface/sprint-bindings?planner_shell_id=" + sel.shell_id +
            "&include_released=1"),
      apiIf("/interface/sprint-alerts?planner_shell_id=" + sel.shell_id),
    ]);
    bindings = b.bindings || [];
    alerts = a.alerts || [];
  } catch { return; }   // surface down — never break the pane over the panel
  if (!pane.isConnected || (!bindings.length && !alerts.length)) return;
  const card = el("div", { className: "card" });
  card.append(el("div", {}, el("b", {}, "Sprint wake")));
  const note = el("div", { className: "if-note" });

  if (bindings.length) {
    const b = bindings[0];   // unreleased first, then most recent
    const doc = b.sprint || {};
    const diag = el("div", { className: "if-diag" });
    const drow = (k, v) => diag.append(el("span", { className: "if-stat" }, k + " ", el("b", {}, String(v ?? "—"))));
    drow("sprint", "#" + b.sprint_doc_id + " " + (doc.title || "?") +
      " · " + (doc.active ? "ACTIVE" : "not-ACTIVE") + (doc.frozen ? " · frozen" : ""));
    drow("binding", "#" + b.binding_id + " " +
      (b.released_at ? "released (" + (b.release_reason || "—") + ")" : "armed"));
    drow("wake", b.wake_state);
    if (b.current_batch)
      drow("batch", "#" + b.current_batch.batch_id + " " + b.current_batch.state);
    if (b.last_batch)
      drow("last outcome", "#" + b.last_batch.batch_id + " " + b.last_batch.state +
        " · " + ifCounts(b.last_batch.items));
    drow("items", ifCounts(b.items));
    if (b.quarantined && b.quarantined.length)
      drow("quarantined", b.quarantined.length +
        " item(s) — " + (b.quarantined[0].error || "wake limit"));
    card.append(diag);
    if (b.park)
      card.append(el("div", { className: "if-note" },
        "PARKED: " + (b.park.reason || "delivery_unknown") +
        (b.park.input_park ? " — the input frame's delivery is unknown; retry needs your verdict" : "")));
    const retry = b.retry || {};
    if (retry.applicable && !b.released_at) {
      const acts = el("div", {});
      const refresh = () => { card.remove(); ifSprintPanel(pane, sel); };
      const doRetry = async (outcome, btn) => {
        btn.disabled = true; note.textContent = "";
        try {
          const r = await apiIf("/interface/sprint-bindings/" + b.binding_id + "/retry",
            "POST", outcome ? { outcome } : {});
          note.textContent = "retried — wake now " + r.wake_state +
            " (" + (r.actions || []).join("; ") + ")";
          setTimeout(refresh, 800);
        } catch (e) {
          note.textContent = (e.code ? e.code + ": " : "") + e.message;
          btn.disabled = false;
        }
      };
      if (retry.needs_outcome) {
        const landed = el("button", { className: "act", type: "button", textContent: "Retry — input landed",
          title: "the parked frame reached the planner — fold it in and requeue the batch as NEW" });
        landed.onclick = () => {
          if (confirm("Confirm the parked input reached the planner's pane. The parked batch closes as audit and its items requeue as a NEW gated batch."))
            doRetry("delivered", landed);
        };
        const lost = el("button", { className: "act", type: "button", textContent: "Retry — input lost",
          title: "the parked frame never landed — drop it and requeue the batch as NEW" });
        lost.onclick = () => {
          if (confirm("Confirm the parked input NEVER reached the planner. The parked batch closes as audit and its items requeue as a NEW gated batch."))
            doRetry("not_delivered", lost);
        };
        acts.append(landed, " ", lost);
      } else {
        const retryBtn = el("button", { className: "act", type: "button", textContent: "Retry wake",
          title: "requeue parked/stalled wake work as a NEW gated batch — the parked batch is never resubmitted" });
        retryBtn.onclick = () => {
          if (confirm("Retry this binding's wake work? The parked batch closes as audit and its items requeue as a NEW gated batch."))
            doRetry(null, retryBtn);
        };
        acts.append(retryBtn);
      }
      card.append(acts);
    }
  }
  if (alerts.length) {
    const list = el("div", { className: "if-diag" });
    for (const a of alerts)
      list.append(el("div", { className: "if-note" },
        "⚠ " + a.severity + " · " + a.reason + " · opened " + ifAge(a.opened_at) + " ago"));
    card.append(list);
  }
  card.append(note);
  pane.append(card);
}
function ifCounts(counts) {
  const parts = Object.entries(counts || {}).map(([k, v]) => k + ":" + v);
  return parts.length ? parts.join(", ") : "—";
}

// Available pane: one primary New chat command (spec Interface Layout — no
// second New-chat control exists for occupied or unreconciled shells). The
// action opens the normal harness/model/effort choices sourced from the live
// catalogue (GET /api/models v3, family-first) using the Default Models
// picker conventions (dmModelPicker). POST /sessions rejects unknown fields
// and accepts only shell_id/harness/model/effort/rows/cols — there is no
// permission-mode field on the API, so none is offered here.
function ifAvailablePane(pane, sel, root) {
  ifDetach();
  const card = el("div", { className: "card if-newchat" },
    el("div", {}, el("b", {}, sel.display_name || sel.shortname), " is available."));
  const open = el("button", { className: "act primary", type: "button", textContent: "New chat" });
  open.onclick = () => { open.remove(); ifNewChatForm(card, sel, root); };
  card.append(open);
  pane.append(card);
}

async function ifNewChatForm(card, sel, root) {
  const msg = el("div", { className: "muted" }, "loading model catalogue…");
  card.append(msg);
  let cat = null;
  try { cat = await api("/models"); } catch { /* typed ids still store as-is */ }
  if (!card.isConnected) return;   // user navigated away mid-fetch
  msg.textContent = cat ? "" :
    "catalogue unreachable — typed ids are stored as-is (advisory).";

  const harnesses = Object.keys((cat && cat.harnesses) || {}).sort();
  const choice = { harness: harnesses[0] || null, model: "", effort: "" };

  const effortSel = el("select", { className: "if-effort", title: "effort" });
  const effortRow = el("div", { className: "dm-row", hidden: true },
    el("span", { className: "dm-harness" }, "effort"), effortSel);
  effortSel.onchange = () => { choice.effort = effortSel.value; };
  const paintEffort = () => {
    const entry = (((cat || {}).harnesses || {})[choice.harness] || { models: [] })
      .models.find((m) => m.id === choice.model);
    const efforts = (entry && entry.supported_efforts) || [];
    choice.effort = "";
    effortSel.replaceChildren(el("option", { value: "" }, "effort: harness default"));
    for (const ef of efforts) {
      const o = el("option", { value: ef }, "effort: " + ef);
      if (ef === entry.default_effort) { o.selected = true; choice.effort = ef; }
      effortSel.append(o);
    }
    effortRow.hidden = !efforts.length;   // effort is unknown for typed/uncatalogued models
  };

  // dmModelPicker reads row.model for the current label and calls save(value)
  // on a pick — `choice` plays the row; save also refreshes the effort list.
  const pickerZone = el("div");
  const buildPicker = () => {
    const picker = dmModelPicker(choice.harness, cat || { harnesses: {} }, choice,
      async (value) => { choice.model = value || ""; paintEffort(); });
    pickerZone.replaceChildren(
      el("div", { className: "dm-row" },
        el("span", { className: "dm-harness" }, "model"), picker.current, picker.input),
      picker.results);
  };

  let harnessCtl;
  if (harnesses.length) {
    harnessCtl = el("select", { title: "harness" });
    for (const h of harnesses) harnessCtl.append(el("option", { value: h }, h));
    harnessCtl.onchange = () => {
      choice.harness = harnessCtl.value; choice.model = "";
      buildPicker(); paintEffort();
    };
  } else {
    harnessCtl = el("input", { type: "text", className: "dm-search",
      placeholder: "harness (e.g. claude)" });
    harnessCtl.oninput = () => { choice.harness = harnessCtl.value.trim() || null; };
  }
  buildPicker();   // effort select stays hidden until a catalogued model is picked

  const start = el("button", { className: "act primary", type: "button", textContent: "Start chat" });
  start.onclick = async () => {
    start.disabled = true; msg.textContent = "";
    const body = { shell_id: sel.shell_id };
    if (choice.harness) body.harness = choice.harness;
    if (choice.model) body.model = choice.model;
    if (choice.effort) body.effort = choice.effort;
    try {
      // 201 → the shell flips to reserved/starting; re-render lands on the
      // session pane. A 202 (ambiguous spawn) re-renders onto the recovery
      // pane, which shows the server's error_detail.
      await apiIf("/interface/sessions", "POST", body);
      await renderInterface(root);
    } catch (e) {   // 409 shell_occupied / unmanaged_harness — show the server's message
      msg.textContent = (e.code ? e.code + ": " : "") + e.message;
      start.disabled = false;
    }
  };
  card.append(
    el("div", { className: "dm-row" },
      el("span", { className: "dm-harness" }, "harness"), harnessCtl),
    pickerZone,
    effortRow,
    start);
}

// Occupied/starting pane: header state line + terminal. Attaches writer-first
// (writer-leases → stream-tickets role=writer → WS); a 409 on the lease means
// another client holds it, so we attach read-only and offer Take-over.
async function ifSessionPane(pane, sel) {
  if (!sel.session_id) {
    ifDetach();
    pane.append(el("div", { className: "card muted" },
      "Session is starting — no session id yet. Reselect the shell to retry."));
    return;
  }
  const sessionId = sel.session_id;
  // Reuse a live attach to the SAME session across re-renders (rail refresh,
  // hash no-op) — the terminal keeps its scrollback; only the DOM is re-parented.
  if (ifAttach && ifAttach.sessionId === sessionId &&
      ifAttach.ws && ifAttach.ws.readyState <= WebSocket.OPEN) {
    pane.append(ifAttach.headEl, ifAttach.termEl);
    ifSprintPanel(pane, sel);
    return;
  }
  ifDetach();
  let sess;
  try { sess = await apiIf("/interface/sessions/" + sessionId); }
  catch (e) { pane.append(el("div", { className: "card" }, "error: " + e.message)); return; }

  const st = {
    harness: sess.harness || sel.harness || "—",
    model: sess.model_route || "harness default",
    lifecycle: sess.lifecycle || "—",
    composer: sess.composer || "unknown",
    writer: sess.writer && sess.writer.held ? "held" : "none",
    clients: sess.clients ?? 0,
    wake: sess.wake_state || "disarmed",
    archive: sess.archive_id ?? null,
    since: sess.occupied_at || sess.created_at || null,
    note: "",
  };
  const headEl = el("div", { className: "if-head" });
  const termEl = el("div", { className: "if-term" });
  const a = { sessionId, shortname: sel.shortname, st, headEl, termEl,
    ws: null, term: null, leaseId: null, leaseToken: null, role: "viewer",
    seq: 1, inflight: 0, lastAck: 0, outBuf: "", awaiting: false, halted: false,
    heartbeat: 0, resizeObs: null, paint: null };
  a.paint = () => ifPaintHeader(a, sel, pane);
  ifAttach = a;
  pane.append(headEl, termEl);
  ifSprintPanel(pane, sel);
  a.paint();

  try {
    try {
      const lease = await apiIf("/interface/writer-leases", "POST",
        { session_id: sessionId, client_id: ifClientId, takeover: false });
      if (ifAttach !== a) return ifReleaseLease(a);   // user moved on mid-attach
      a.leaseId = lease.lease_id;
      a.leaseToken = lease.lease_token;
      a.role = "writer";
      // The lease reseeds input seqs from the session's forwarded_seq + 1.
      a.seq = lease.next_input_seq ?? 1;
      a.st.writer = "active";
    } catch (e) {
      if (e.status !== 409) throw e;
      a.st.writer = "held";   // read-only attach; Take-over button in the header
    }
    a.paint();
    const t = await apiIf("/interface/stream-tickets", "POST",
      { session_id: sessionId, role: a.role, client_id: ifClientId,
        ...(a.leaseToken ? { lease_token: a.leaseToken } : {}) });
    if (ifAttach !== a) return ifReleaseLease(a);
    ifOpenStream(a, t.ticket);
  } catch (e) {
    if (ifAttach !== a) return ifReleaseLease(a);
    a.st.note = "attach failed: " + e.message;
    a.paint();
  }
}
function ifReleaseLease(a) {
  if (!a.leaseId || !ifCsrf) return;
  fetch("/api/interface/writer-leases/" + a.leaseId, {
    method: "DELETE", credentials: "same-origin", keepalive: true,
    headers: { "X-CSRF": ifCsrf, "Idempotency-Key": crypto.randomUUID() },
  }).catch(() => {});
  a.leaseId = null;
}

// Session age from a UTC timestamp — SQLite datetime('now') shape
// ("YYYY-MM-DD HH:MM:SS") or ISO-8601; both parse as UTC here.
function ifAge(ts) {
  if (!ts) return "—";
  const s0 = String(ts);
  const t = new Date(s0.includes("T") ? s0 : s0.replace(" ", "T") + "Z");
  if (isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t.getTime()) / 1000);
  if (s < 90) return Math.floor(s) + "s";
  if (s < 5400) return Math.floor(s / 60) + "m";
  if (s < 129600) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

function ifPaintHeader(a, sel, pane) {
  const st = a.st;
  const stat = (k, v) => el("span", { className: "if-stat" }, k + " ", el("b", {}, String(v)));
  // Spec Interface Layout header: harness/model, archive/session age, writer
  // or read-only state, draft (composer) state, sprint wake state, then the
  // exact recovery actions. idle/busy/approval/user_input stay lifecycle
  // details HERE — they never replace occupancy in the rail.
  const head = [
    stat("harness", st.harness),
    stat("model", st.model),
    stat("session", "#" + a.sessionId + (st.archive != null ? " · arc #" + st.archive : "")),
    stat("age", ifAge(st.since)),
    stat("lifecycle", st.lifecycle),
    stat("composer", st.composer),
    stat("writer", st.writer === "active" ? "you" : st.writer === "held" ? "read-only" : st.writer),
    stat("clients", st.clients),
    stat("wake", st.wake),
  ];
  if (st.writer === "held" || st.writer === "revoked") {
    const take = el("button", { className: "act", type: "button", textContent: "Take-over",
      title: "explicitly take the writer lease — the current writer turns read-only" });
    take.onclick = () => {
      if (confirm("Take control of this session? The current writer (another tab or CLI) becomes read-only."))
        ifTakeover(a);
    };
    head.push(take);
  }
  if (st.composer === "dirty" || st.composer === "unknown") {
    const cert = el("button", { className: "act", type: "button", textContent: "certify clean" });
    cert.onclick = async () => {
      cert.disabled = true;
      try {
        await apiIf("/interface/clean-certifications", "POST",
          { session_id: a.sessionId, client_id: ifClientId, client_seq: a.lastAck });
        st.composer = "clean";
      } catch (e) { st.note = "certify failed: " + e.message; }
      a.paint();
    };
    head.push(cert);
  }
  const end = el("button", { className: "act", type: "button", textContent: "End chat",
    title: "explicit, confirmed — graceful first; force unlocks only after a graceful timeout" });
  end.onclick = () => ifEndChat(a, sel, pane, end);
  head.push(end);
  if (st.note) head.push(el("span", { className: "if-note" }, st.note));
  a.headEl.replaceChildren(...head);
}

// End chat (spec Workflow 9): explicit + confirmed, graceful first. Force is
// a SEPARATE action that exists only after the graceful request timed out —
// the API enforces the same gate (force_requires_graceful_timeout) and the
// prompt names the exact PID/generation from its response. The shell returns
// to available only when the API says terminated; identity_mismatch fails
// closed into unreconciled/lost, so we re-render onto the recovery pane.
async function ifEndChat(a, sel, pane, btn) {
  if (!confirm(`End chat with ${sel.display_name || sel.shortname}? This terminates session #${a.sessionId}.`)) return;
  if (btn) btn.disabled = true;
  const root = pane.closest(".view");
  let r;
  try { r = await apiIf("/interface/termination-requests", "POST", { session_id: a.sessionId, force: false }); }
  catch (e) {
    if (e.status === 409 && e.body && e.body.reason) r = e.body;
    else { a.st.note = "end chat failed: " + e.message; a.paint(); return; }
  }
  if (!r.terminated && r.reason === "identity_mismatch") {
    if (root) return renderInterface(root);
    return;
  }
  if (!r.terminated && r.reason === "graceful_timeout") {
    if (!confirm(`Graceful stop timed out. Force-kill PID ${r.pid} (generation ${r.generation})?`)) {
      a.st.note = "graceful stop timed out — End chat again to retry, or confirm the force kill.";
      a.paint();
      return;
    }
    try { r = await apiIf("/interface/termination-requests", "POST", { session_id: a.sessionId, force: true }); }
    catch (e) { a.st.note = "force kill failed: " + e.message; a.paint(); return; }
  }
  if (r.terminated) {
    ifDetach();
    if (root) renderInterface(root);
  } else { a.st.note = "not terminated: " + (r.reason || "?"); a.paint(); }
}

// Take-over: fresh lease with takeover:true (new idempotency key — different
// body), then re-open the stream as writer with the new lease token.
async function ifTakeover(a) {
  try {
    const lease = await apiIf("/interface/writer-leases", "POST",
      { session_id: a.sessionId, client_id: ifClientId, takeover: true });
    if (ifAttach !== a) return ifReleaseLease(a);
    a.leaseId = lease.lease_id;
    a.leaseToken = lease.lease_token;
    a.role = "writer";
    a.seq = lease.next_input_seq ?? 1;
    a.inflight = 0; a.outBuf = ""; a.awaiting = false; a.halted = false;
    a.st.writer = "active";
    a.paint();
    const t = await apiIf("/interface/stream-tickets", "POST",
      { session_id: a.sessionId, role: "writer", client_id: ifClientId, lease_token: a.leaseToken });
    if (ifAttach !== a) return ifReleaseLease(a);
    clearInterval(a.heartbeat);
    a.resizeObs?.disconnect();
    try { a.ws?.close(); } catch { /* already closed */ }
    a.term?.dispose();
    a.termEl.replaceChildren();
    ifOpenStream(a, t.ticket);
  } catch (e) { a.st.note = "take-over failed: " + e.message; a.paint(); }
}

// Open the sc-term.v1 stream: 0x00 output → write, 0x04 snapshot → reset+write;
// keystrokes go out as 0x01 ‖ seq:u64be ‖ payload, one unacked frame at a time.
function ifOpenStream(a, ticket) {
  if (typeof Terminal === "undefined") {   // deferred vendor script not ready yet
    a.st.note = "terminal library still loading — refresh the page";
    a.paint();
    return;
  }
  const term = new Terminal({ convertEol: false, cursorBlink: true,
    fontFamily: "ui-monospace, monospace" });
  a.term = term;
  term.open(a.termEl);
  const ws = new WebSocket(
    (location.protocol === "https:" ? "wss://" : "ws://") + location.host +
    "/api/interface/session-streams/" + a.sessionId + "?ticket=" + encodeURIComponent(ticket),
    "sc-term.v1");
  ws.binaryType = "arraybuffer";
  a.ws = ws;
  ws.onmessage = (ev) => {
    if (ifAttach !== a) return;
    if (typeof ev.data === "string") return ifControl(a, JSON.parse(ev.data));
    const b = new Uint8Array(ev.data);
    if (!b.length) return;
    if (b[0] === 0x00) a.term.write(b.subarray(1));
    else if (b[0] === 0x04) { a.term.reset(); a.term.write(b.subarray(1)); }
  };
  ws.onclose = () => {
    if (ifAttach !== a) return;
    a.st.note = "stream closed — reselect the shell to reattach";
    if (a.role === "writer") a.st.writer = "none";
    a.paint();
  };
  a.heartbeat = setInterval(() => {
    if (a.ws && a.ws.readyState === WebSocket.OPEN) a.ws.send(JSON.stringify({ type: "heartbeat" }));
  }, 10000);
  if (a.role === "writer") term.onData((d) => ifSendInput(a, d));
  term.onResize(({ rows, cols }) => ifSendResize(a, rows, cols));
  // No FitAddon is vendored — estimate the char grid from the container and
  // resize the terminal to fill it; the onResize hook above forwards it.
  const fit = () => {
    const w = a.termEl.clientWidth, h = a.termEl.clientHeight;
    if (!w || !h) return;
    const cols = Math.max(20, Math.floor((w - 12) / 9));
    const rows = Math.max(4, Math.floor((h - 12) / 17));
    if (cols !== term.cols || rows !== term.rows) term.resize(cols, rows);
  };
  a.resizeObs = new ResizeObserver(fit);
  a.resizeObs.observe(a.termEl);
  fit();
}

function ifSendInput(a, data) {
  if (a.halted || a.role !== "writer") return;
  a.outBuf += data;   // one unacked frame per writer — buffer while awaiting ack
  ifFlush(a);
}
function ifFlush(a) {
  if (a.awaiting || a.halted || !a.outBuf) return;
  if (!a.ws || a.ws.readyState !== WebSocket.OPEN) return;
  const payload = new TextEncoder().encode(a.outBuf);
  const frame = new Uint8Array(9 + payload.length);
  frame[0] = 0x01;
  new DataView(frame.buffer).setBigUint64(1, BigInt(a.seq));
  frame.set(payload, 9);
  a.ws.send(frame);
  a.inflight = a.seq;
  a.seq++;
  a.outBuf = "";
  a.awaiting = true;
}
function ifSendResize(a, rows, cols) {
  if (!a.ws || a.ws.readyState !== WebSocket.OPEN) return;
  const frame = new Uint8Array(5);
  frame[0] = 0x03;
  new DataView(frame.buffer).setUint16(1, rows);
  new DataView(frame.buffer).setUint16(3, cols);
  a.ws.send(frame);
}

// This client's writer lease is gone (taken over elsewhere): flip read-only
// IMMEDIATELY with a clear notice (spec Workflow 6).
function ifRevoked(a) {
  a.role = "viewer"; a.leaseId = null; a.leaseToken = null;
  a.awaiting = false; a.halted = false; a.outBuf = "";
  a.st.writer = "held";
  a.st.note = "control was taken by another client — you are now read-only (Take-over to reclaim)";
}

function ifControl(a, m) {
  switch (m.type) {
    case "input_ack":   // may carry replayed:true — either way the frame landed
      if (m.seq === a.inflight) { a.awaiting = false; a.lastAck = m.seq; ifFlush(a); }
      break;
    case "input_reject":
      a.awaiting = false;
      // writer_revoked = a takeover revoked our lease; the runtime learns of
      // it at the next keystroke and rejects the frame. Same flip as a
      // writer-state revoke — not a halt.
      if (m.reason === "writer_revoked" && a.role === "writer") {
        ifRevoked(a);
      } else {   // seqs are session-scoped; stop until the next attach
        a.halted = true;
        a.st.note = `input rejected (seq ${m.seq}): ${m.reason || "?"} — reattach to resume`;
      }
      a.paint();
      break;
    case "writer":
      // Server states (interface_runtime.writer_control): active | held |
      // none — there is no "revoked" frame. A non-active state while we still
      // believe we hold the lease IS the revocation signal.
      if (a.role === "writer" && m.state !== "active") ifRevoked(a);
      else a.st.writer = m.state;
      a.paint();
      break;
    case "lifecycle":
      if (m.lifecycle != null) a.st.lifecycle = m.lifecycle;
      if (m.composer != null) a.st.composer = m.composer;
      a.paint();
      break;
    case "resync":   // the 0x04 snapshot that follows repaints the screen
    case "heartbeat":
      break;
    case "error":
      a.st.note = "error: " + (m.code || "?");
      a.paint();
      break;
  }
}

// ── Tabs + boot ────────────────────────────────────────────────────────────────
const VIEWS = {
  interface: ["#view-interface", renderInterface],
  shells: ["#view-shells", renderShells],
  skills: ["#view-skills", renderSkills],
  roadmap: ["#view-roadmap", renderRoadmap],
  docs: ["#view-docs", renderDocs],
  flags: ["#view-flags", renderFlags],
  worktrees: ["#view-worktrees", renderWorktrees],
  map: ["#view-map", renderMap],
  analytics: ["#view-analytics", renderAnalytics],
  scripts: ["#view-scripts", renderScripts],
};
async function load(tab) {
  const [sel, fn] = VIEWS[tab];
  try { await fn($(sel)); } catch (e) { $(sel).replaceChildren(el("div", { className: "card" }, "error: " + e.message)); }
}
function show(tab) {
  for (const b of document.querySelectorAll("nav button")) b.classList.toggle("active", b.dataset.tab === tab);
  for (const k of Object.keys(VIEWS)) $(VIEWS[k][0]).hidden = k !== tab;
  if (tab !== "interface") ifDetach();   // leaving the tab drops the stream + lease
  load(tab);
}
// Hash routing: the active tab lives in the URL (#roadmap), so a refresh stays
// put (and re-fetches that tab) instead of snapping back to Shells. Tabs set the
// hash; hashchange drives show — back/forward and deep links work too. The
// roadmap tab carries its sub-view in the hash: #roadmap (board) | #roadmap-flow.
// The interface tab carries its selected shell: #interface | #interface/DEV3.
function routeFromHash() {
  const raw = location.hash.slice(1);
  if (raw === "roadmap" || raw.startsWith("roadmap-")) {
    roadmapView = raw === "roadmap-flow" ? "flow" : "board";
    show("roadmap");
    return;
  }
  if (raw === "interface" || raw.startsWith("interface/")) {
    ifSelected = raw.includes("/") ? decodeURIComponent(raw.slice(raw.indexOf("/") + 1)) : null;
    show("interface");
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
