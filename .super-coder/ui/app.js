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

async function api(path, method = "GET", body, extraHeaders = {}) {
  const r = await fetch("/api" + path, {
    method, headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...extraHeaders,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  // maintenance scripts report failure as {ok:false, output:<step trace>} with no
  // `error` key — the trace names the refusing guard and the remedy, so it is
  // the message, not statusText.
  if (!r.ok) {
    const err = data.error;
    const message = typeof err === "object" && err
      ? [err.code, err.message].filter(Boolean).join(": ")
      : (err || data.output || r.statusText);
    throw new Error(message);
  }
  return data;
}

function requestKey() {
  return globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function chatApi(path, method = "GET", body, idempotencyKey) {
  const headers = body === undefined ? {} : { "Content-Type": "application/json" };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  let response;
  try {
    response = await fetch("/api" + path, {
      method, headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    const error = new Error("The conversation service could not be reached.");
    error.code = "NETWORK_ERROR";
    error.cause = cause;
    throw error;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.error || {};
    const error = new Error(detail.message || response.statusText);
    error.code = detail.code || "CONVERSATION_REQUEST_FAILED";
    error.details = detail.details || {};
    error.status = response.status;
    throw error;
  }
  return data;
}

function toast(msg) {
  const t = el("div", { className: "toast" }, msg);
  document.body.append(t);
  // Multi-line maintenance traces need longer than one-liners.
  setTimeout(() => t.remove(), Math.min(12000, Math.max(4000, String(msg).length * 30)));
}
function setStatus(s) { $("#status").textContent = s; }

// ── Skill sections ──────────────────────────────────────────────────────────
// One grouping rule for the Shells skill viewer AND Skill Assignments. "Repo
// skills" are fork-local (origin='repo', derived server-side from the snapshot
// rule: name not under engine assets/skills) and always lead; engine skills
// section by their category.
const SECTION_ORDER = ["repo", "substrate", "craft"];
const SECTION_LABEL = { repo: "Repo skills", substrate: "Substrate", craft: "Craft", other: "Other" };
const SECTION_NOTE = {
  repo: "Authored in this repo — not engine catalogue. Durable in the local engine DB and gitignored snapshot; see the local_skill_management skill.",
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
// Skills | Skill Assignments | Default Models sub-tabs. Flat panels,
// accordions, popover pickers, and a unified edit modal.
let selectedShell = null;
let shellTab = "harness";     // 'harness' | 'skills' | 'assignments' | 'models'
let activeSkillId = null;     // skill-viewer selection; reset on shell switch
let shellRenderEpoch = 0;     // discard stale async renders after re-navigation
const SHELL_TAB_HASH = {
  harness: "shells",
  skills: "shells-skills",
  assignments: "shells-skill-assignments",
  models: "shells-default-models",
};

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
// the title + an optional readout; footer content uses explicit physical
// start/end slots. Returns the close function.
let modalSequence = 0;
function openModal({
  title, headExtra, bodyNode, footerStart, footerEnd, width = 650, height = 700,
}) {
  const priorFocus = document.activeElement;
  const overlay = el("div", { className: "modal-overlay" });
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    overlay.remove();
    if (priorFocus?.isConnected && typeof priorFocus.focus === "function") priorFocus.focus();
  };
  overlay.closeModal = close;
  overlay.onmousedown = (e) => { if (e.target === overlay) close(); };
  const dlg = el("div", { className: "modal" });
  const titleId = `modal-title-${++modalSequence}`;
  dlg.setAttribute("role", "dialog");
  dlg.setAttribute("aria-modal", "true");
  dlg.setAttribute("aria-labelledby", titleId);
  dlg.style.width = width + "px";
  dlg.style.height = height + "px";
  const head = el("div", { className: "modal-head" },
    el("div", { className: "modal-title", id: titleId }, title));
  if (headExtra) head.append(headExtra);
  dlg.append(head, el("div", { className: "modal-body" }, bodyNode));
  if (footerStart || footerEnd) {
    const foot = el("div", { className: "modal-foot" });
    if (footerStart) foot.append(el("div", { className: "modal-foot-start" }, footerStart));
    if (footerEnd) foot.append(el("div", { className: "modal-foot-end" }, footerEnd));
    dlg.append(foot);
  }
  overlay.append(dlg);
  document.body.append(overlay);
  return close;
}

// Action dialogs name intent instead of physical position. Dismissal is always
// first/left and the primary or destructive action is always last/right.
function openActionModal({ dismissNode, actionNode, ...modal }) {
  return openModal({ ...modal, footerStart: dismissNode, footerEnd: actionNode });
}

// Unified edit modal — 650×700, Cancel bottom-left / Save bottom-right,
// live ~tokens / chars readout in the header.
function openEditModal({ title, value, onSave }) {
  const counter = el("div", { className: "modal-count" });
  const ta = el("textarea", { value: value || "" });
  const upd = () => { counter.textContent = `~${fmt(approxTokens(ta.value))} tokens / ${fmt(ta.value.length)} chars`; };
  ta.oninput = upd; upd();
  const save = el("button", { className: "act primary", type: "button", textContent: "Save" });
  const cancel = el("button", { className: "act", type: "button", textContent: "Cancel" });
  const close = openActionModal({
    title, headExtra: counter, bodyNode: ta, dismissNode: cancel, actionNode: save,
  });
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
      footerStart: rawBtn, footerEnd: closeBtn,
      width: 800, height: 650,
    });
    closeBtn.onclick = close;
  } catch (e) { toast("error: " + e.message); }
}

// New-shell form in a 600×300 modal — Cancel bottom-left, Create bottom-right.
function openNewShellModal(templates, root) {
  const fl = el("select", {});
  for (const t of templates)
    fl.append(el("option", { value: t.flavor, textContent: `${t.flavor} — ${t.role}` }));
  fl.append(el("option", {
    value: "",
    textContent: "Bespoke — custom skill pack",
  }));
  const nm = el("input", { type: "text", placeholder: "name (e.g. Arch)" });
  const create = el("button", { className: "act primary", type: "button", textContent: "Create" });
  const cancel = el("button", { className: "act", type: "button", textContent: "Cancel" });
  const form = el("div", { className: "modal-form" },
    el("span", { className: "k" }, "shell type"), fl,
    el("span", { className: "k" }, "name"), nm);
  const close = openActionModal({ title: "New shell", bodyNode: form,
    dismissNode: cancel, actionNode: create, width: 600, height: 300 });
  create.onclick = async () => {
    if (!nm.value.trim()) return toast("name required");
    create.disabled = true; create.textContent = "Creating…";
    try {
      const r = await api("/shells", "POST", {
        flavor: fl.value || null,
        name: nm.value.trim(),
      });
      selectedShell = r.shell_id; activeSkillId = null;
      close(); setStatus(`shell created — ${r.shortname}`); renderShells(root);
    } catch (e) { toast("error: " + e.message); create.disabled = false; create.textContent = "Create"; }
  };
  cancel.onclick = close;
  nm.focus();
}

async function renderShells(root) {
  const epoch = ++shellRenderEpoch;
  const { shells } = await api("/shells");
  if (epoch !== shellRenderEpoch) return;
  const { templates } = await api("/shell-templates");
  if (epoch !== shellRenderEpoch) return;
  root.replaceChildren();
  if (!shells.length) { root.append(el("div", { className: "card muted" }, "No shells.")); return; }
  if (selectedShell == null || !shells.find((s) => s.shell_id === selectedShell))
    selectedShell = shells[0].shell_id;
  const s = await api("/shells/" + selectedShell);
  if (epoch !== shellRenderEpoch) return;
  root.replaceChildren();

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
  if (shellTab === "assignments" || shellTab === "models")
    sub.classList.add("subbar-inert");
  root.append(sub);

  // Harness / Skills are scoped to the selected shell. Skill Assignments and
  // Default Models are fork-global views nested here to keep shell setup in
  // one place. Hash navigation gives every section a reload-safe URL.
  const tabs = el("div", { className: "vtabs" });
  for (const [key, label] of [["harness", "Harness"], ["skills", "Skills"],
                              ["assignments", "Skill Assignments"],
                              ["models", "Default Models"]]) {
    const b = el("button", { className: shellTab === key ? "active-tab" : "", type: "button", textContent: label });
    b.onclick = () => { location.hash = SHELL_TAB_HASH[key]; };
    tabs.append(b);
  }
  root.append(tabs);

  const pane = el("div", {
    className: "shell-pane" + (shellTab === "assignments" ? " skill-assignments" : ""),
  });
  root.append(pane);
  if (shellTab === "harness") renderHarness(pane, s);
  else if (shellTab === "models") renderDefaultModels(pane, s);
  else if (shellTab === "assignments") renderSkillAssignments(pane);
  else renderSkillViewer(pane, s);
}

// Default Models — the flavor_defaults launch matrix: per flavor, a model per
// harness and ONE starred default harness (the two launch defaults run.py
// resolves at boot). Fork-global config — the selected shell's flavor leads,
// but the matrix is the same from any shell.
//
// One shared harness-first picker for Default Models.
// Focus opens Harness default + every exact locally available route for that
// harness. Search only filters that list; it is never itself a selectable
// value. Arrow keys move the highlight, Enter chooses it, and Escape/outside
// click closes without changing the current selection.

function dmModelPicker(harness, cat, row, save) {
  const data = cat.harnesses?.[harness] || { models: [] };
  const currentRoute = (data.models || []).find((m) => m.id === row.model);
  const currentAvailable = !row.model || (
    currentRoute && currentRoute.availability === "available" && !cat.stale);
  const current = el("span", {
    className: "dm-current" + (row.model ? "" : " dm-unset") +
      (currentAvailable ? "" : " dm-stale"),
    textContent: row.model
      ? row.model + (currentAvailable ? "" : " (stale)")
      : "Harness default",
    title: currentAvailable
      ? (currentRoute
        ? `${currentRoute.availability} · ${currentRoute.source || "unknown source"}`
        : "")
      : "This stored route is unavailable. Choose an available model or Harness default before launch.",
  });
  const input = el("input", { className: "dm-search",
                              placeholder: "Search models for " + harness,
                              role: "combobox", ariaExpanded: "false" });
  const results = el("div", { className: "dm-results", hidden: true });
  let open = false, highlighted = 0, choices = [];
  // Moving the highlight must never go through paint(). paint() rebuilds every
  // card node, so a repaint driven by hover destroys the card under the cursor
  // — mousedown lands on one node, the rebuild detaches it, mouseup lands on
  // its replacement, and with no common ancestor the browser never fires a
  // click. That is why picking a model by mouse did nothing and only
  // search-then-Enter worked. The highlight is presentational: move it in
  // place. paint() stays for changes that genuinely alter the list.
  let applyHighlight = () => {};
  const setHighlight = (i) => { highlighted = i; applyHighlight(); };

  const close = () => {
    open = false; highlighted = 0; input.value = "";
    input.ariaExpanded = "false"; paint();
  };
  const pick = async (value) => {
    try {
      await save(value);
    } catch (e) {
      current.title = e.message;
      return;
    }
    row.model = value || null;
    current.textContent = value || "Harness default";
    current.classList.toggle("dm-unset", !value);
    current.classList.remove("dm-stale");
    close();
  };
  const routeSub = (m) => {
    const efforts = m.supported_efforts || [];
    const route = efforts.includes("high")
      ? "local · high-effort route" : "local route";
    return [route, m.source, m.release_date].filter(Boolean).join(" · ");
  };

  const paint = () => {
    results.textContent = "";
    applyHighlight = () => {};   // the list this closed over is detached now
    if (!open) { results.hidden = true; return; }
    const q = input.value.trim().toLowerCase();
    const hit = (m) => !q || [m.id, m.name, m.family]
      .some((s) => (s || "").toLowerCase().includes(q));
    const models = cat.stale ? [] : (data.models || []).filter(
      (m) => m.availability === "available" && hit(m));
    choices = [
      ...(!q || "harness default".includes(q)
        ? [{ value: null, label: "Harness default", sub: "clear the model override" }]
        : []),
      ...models.map((m) => ({ value: m.id, label: m.id, sub: routeSub(m) })),
    ];
    highlighted = Math.max(0, Math.min(highlighted, choices.length - 1));
    results.append(el("div", { className: "dm-sect" },
      `${models.length} available model${models.length === 1 ? "" : "s"} for ${harness}`));
    const list = el("div", { className: "dm-cardlist", role: "listbox" });
    choices.forEach((choice, i) => {
      const card = el("button", {
        className: "dm-mcard", type: "button", role: "option",
        title: choice.value || "Harness default",
      });
      card.append(el("b", {}, choice.label),
        el("span", { className: "dm-mcard-sub" }, choice.sub));
      card.onmouseenter = () => setHighlight(i);
      card.onclick = () => pick(choice.value);
      list.append(card);
    });
    applyHighlight = () => {
      // Index loop, not forEach — `children` is a live HTMLCollection in the
      // browser and has no forEach, so an array method would silently no-op.
      for (let j = 0; j < list.children.length; j += 1) {
        const node = list.children[j];
        node.classList.toggle("dm-highlight", j === highlighted);
        node.ariaSelected = String(j === highlighted);
      }
      list.children[highlighted]?.scrollIntoView({ block: "nearest" });
    };
    results.append(list);
    applyHighlight();
    results.hidden = false;
  };

  input.onfocus = () => {
    if (!open) { open = true; highlighted = 0; input.ariaExpanded = "true"; paint(); }
  };
  input.oninput = () => { highlighted = 0; paint(); };
  input.onkeydown = (e) => {
    if (e.key === "Escape") { close(); input.blur(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const delta = e.key === "ArrowDown" ? 1 : -1;
      setHighlight(Math.max(0, Math.min(highlighted + delta, choices.length - 1)));
      return;
    }
    if (e.key === "Enter" && choices[highlighted]) {
      e.preventDefault();
      pick(choices[highlighted].value);
    }
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
  try { cat = await api("/models"); } catch { /* picker shows Harness default only */ }

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
        } catch (e) { toast("error: " + e.message); throw e; }
      });
      panel.append(el("div", { className: "dm-row" },
        star, el("span", { className: "dm-harness" }, h),
        picker.current, picker.input));
      panel.append(picker.results);   // full-width, collapsed until typed into
    }
    root.append(panel);
  }
  root.append(el("div", { className: "dm-note" },
    "★ = default harness at launch. Model overrides must be exact locally available routes; Harness default clears the override."));
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
  const grantPath = (skillId) => s.flavor
    ? `/flavors/${encodeURIComponent(s.flavor)}/skills/${skillId}`
    : `/shells/${s.shell_id}/skills/${skillId}`;
  for (const k of skills) {
    const row = el("div", { className: "gmenu-item" + (k.skill_id === activeSkillId ? " active-row" : "") });
    const tog = el("button", { className: "gmenu-check", type: "button",
      title: k.granted ? "Revoke" : "Grant", textContent: k.granted ? "☑" : "☐" });
    tog.onclick = async () => {
      try {
        await api(grantPath(k.skill_id), "PUT", { granted: !k.granted });
        k.granted = k.granted ? 0 : 1;
        tog.textContent = k.granted ? "☑" : "☐";
        tog.title = k.granted ? "Revoke" : "Grant";
        setStatus(s.flavor
          ? `${s.flavor} flavor pack updated`
          : `${s.display_name} Bespoke pack updated`);
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
  root.append(
    el("div", { className: "muted note" },
      s.flavor
        ? `Shared ${s.flavor} pack — changes apply to every ${s.flavor} shell.`
        : `Bespoke pack — changes apply only to ${s.display_name}.`),
    el("div", { className: "viewer-head" }, microlabel("Skill Viewer"), wrap, rawBtn));
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

// ── Skill Assignments (catalogue, sectioned) ─────────────────────────────────
async function renderSkillAssignments(root) {
  const { skills, shells, flavors } = await api("/skills");
  const bespokeShells = shells.filter((sh) => !sh.flavor);
  root.replaceChildren();
  root.append(el("div", { className: "muted" },
    "Assign each skill once per standard flavor. Every shell of that flavor inherits the same pack. Bespoke shells remain individually assignable."));
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
    for (const s of sec.skills) card.append(skillRow(s, flavors, bespokeShells));
    wrap.append(card);
    root.append(wrap);
  }
}

function skillRow(s, flavors, bespokeShells) {
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

  // One row per standard flavor, then one row per Bespoke shell.
  const gr = el("div", { className: "grants" });
  gr.append(el("label", { className: "k", textContent: "assigned to" }));
  const list = el("div", { className: "grant-list" });
  list.append(el("div", { className: "k", textContent: "Standard flavors" }));
  for (const fl of flavors) {
    const sw = toggleSwitch(s.granted_flavors.includes(fl.flavor), async (next, cb) => {
      try {
        await api(`/flavors/${encodeURIComponent(fl.flavor)}/skills/${s.skill_id}`,
          "PUT", { granted: next });
        setStatus(`${fl.flavor} flavor pack updated`);
        const i = s.granted_flavors.indexOf(fl.flavor);
        if (next && i < 0) s.granted_flavors.push(fl.flavor);
        if (!next && i >= 0) s.granted_flavors.splice(i, 1);
      } catch (e) { toast("error: " + e.message); cb.checked = !next; }
    });
    list.append(el("div", { className: "grant-row" },
      sw,
      el("span", { className: "grant-name" }, fl.flavor,
        el("span", { className: "muted", textContent: fl.role ? " · " + fl.role : "" }))));
  }
  if (bespokeShells.length)
    list.append(el("div", { className: "k", textContent: "Bespoke shells" }));
  for (const sh of bespokeShells) {
    const sw = toggleSwitch(s.granted_shells.includes(sh.shell_id), async (next, cb) => {
      try {
        await api(`/shells/${sh.shell_id}/skills/${s.skill_id}`, "PUT", { granted: next });
        setStatus(`${sh.display_name} Bespoke pack updated`);
        const i = s.granted_shells.indexOf(sh.shell_id);
        if (next && i < 0) s.granted_shells.push(sh.shell_id);
        if (!next && i >= 0) s.granted_shells.splice(i, 1);
      } catch (e) { toast("error: " + e.message); cb.checked = !next; }
    });
    list.append(el("div", { className: "grant-row" },
      sw,
      el("span", { className: "grant-name" }, sh.display_name,
        el("span", { className: "muted",
          textContent: sh.shortname ? " /" + sh.shortname : "" }))));
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
let roadmapFeatureId = null;          // exact #roadmap-feature-<id> modal route
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
  if (roadmapFeatureId !== null) {
    const requested = roadmapFeatureId;
    roadmapFeatureId = null;
    const feature = buckets.flatMap((bucket) => bucket.features)
      .find((item) => item.feature_id === requested);
    if (feature) openFeatureModal(feature, candidates, projects);
    else toast(`feature #${requested} not found`);
  }
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
// ⤢ button). Cancel bottom-left / Save bottom-right; reloads the roadmap on save.
function openFeatureModal(f, candidates = [], projects = []) {
  const { node, save } = featureForm(f, candidates, projects);
  const saveBtn = el("button", { className: "act primary", type: "button", textContent: "Save" });
  const cancel = el("button", { className: "act", type: "button", textContent: "Cancel" });
  const close = openActionModal({
    title: (f.title || "(untitled)") + "  #" + f.feature_id,
    bodyNode: node, dismissNode: cancel, actionNode: saveBtn,
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

// Shared create/edit flag form — roomy description, dismissal left, action right.
function openFlagModal(features, flag = null) {
  const editing = Boolean(flag);
  const name = el("input", {
    type: "text", placeholder: "display name (e.g. SC-001)", value: flag?.display_name || "",
  });
  const desc = el("textarea", {
    rows: 8, placeholder: "[Area] description | Blocker for: …", value: flag?.description || "",
  });
  const feat = el("select", {});
  feat.append(el("option", {
    value: "", selected: flag?.feature_id == null, textContent: "— no feature —",
  }));
  for (const f of features) feat.append(el("option", {
    value: f.feature_id,
    selected: String(f.feature_id) === String(flag?.feature_id),
    textContent: `#${f.feature_id} ${f.title}`,
  }));
  const prio = el("select", {});
  for (const p of ["High", "Medium", "Low"]) prio.append(el("option", {
    value: p, selected: p === (flag?.priority || "Medium"), textContent: p,
  }));
  const actionLabel = editing ? "Save" : "Create";
  const action = el("button", {
    className: "act primary", type: "button", textContent: actionLabel,
  });
  const cancel = el("button", { className: "act", type: "button", textContent: "Cancel" });
  const form = el("div", { className: "modal-form" },
    el("span", { className: "k" }, "name"), name,
    el("span", { className: "k" }, "description"), desc,
    el("span", { className: "k" }, "feature"), feat,
    el("span", { className: "k" }, "priority"), prio);
  const close = openActionModal({
    title: editing ? `Edit flag #${flag.flag_id}` : "New flag",
    bodyNode: form, dismissNode: cancel, actionNode: action,
    width: 600, height: 520,
  });
  action.onclick = async () => {
    if (!desc.value) return toast("description required");
    action.disabled = true; action.textContent = editing ? "Saving…" : "Creating…";
    const payload = {
      display_name: name.value || null,
      description: desc.value,
      feature_id: feat.value || null,
      priority: prio.value,
    };
    try {
      await api(editing ? "/flags/" + flag.flag_id : "/flags", editing ? "PATCH" : "POST", payload);
      close(); setStatus(editing ? "flag saved" : "flag created"); load("flags");
    } catch (e) {
      toast("error: " + e.message);
      action.disabled = false; action.textContent = actionLabel;
    }
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
  newBtn.onclick = () => openFlagModal(features);
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
      for (const f of list) c.append(flagRow(f, features));
      results.append(c);
    }
  }
  draw();
}

function flagRow(f, features) {
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

  const actions = el("div", { className: "flag-actions" });
  if (f.resolved) {
    body.append(el("div", { className: "tag" }, `resolved ${f.resolved_date || ""} — ${f.resolution_notes || ""}`));
  } else {
    const btn = el("button", { className: "act", type: "button", textContent: "resolve" });
    btn.onclick = async () => {
      const notes = prompt("Resolution notes:");
      if (notes === null) return;
      try { await api("/flags/" + f.flag_id, "PATCH", { resolved: 1, resolution_notes: notes }); setStatus("flag resolved"); load("flags"); }
      catch (e) { toast("error: " + e.message); }
    };
    actions.append(btn);
  }
  const edit = el("button", { className: "act flag-edit", type: "button", textContent: "edit" });
  edit.onclick = (e) => {
    e.preventDefault(); e.stopPropagation();
    openFlagModal(features, f);
  };
  actions.append(edit);
  body.append(actions);
  row.append(body);
  return row;
}

// ── Scripts ─────────────────────────────────────────────────────────────────────
async function renderScripts(root) {
  const { scripts } = await api("/scripts");
  root.replaceChildren();
  root.append(el("div", { className: "muted" },
    "Run a maintenance script. Output appears below it. Per-instance DB edits → Save locally to refresh the ignored snapshot and flat renders."));

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
  const close = openActionModal({
    title: "Windows Test VM", width: 680, height: 760,
    bodyNode: el("div", {}, form, el("div", { className: "modal-form-foot" }, runAll), note, results),
    dismissNode: cancel, actionNode: save,
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
let anView = "tokens";    // 'tokens' | 'quota' — sub-view, carried in the hash

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

function anSessionCard(c) {
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
  if (c.status !== "ok") meta.push(["status", c.status]);
  if (meta.length) body.append(statRow(meta));
  body.append(el("code", { className: "sess-ref" }, c.harness_session_ref));
  row.append(body);
  return row;
}

async function anTokenSection(root) {
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
    anTokenSection(root);
  };
  const rangeSeg = el("div", { className: "filters seg an-range" });
  for (const [label, days] of AN_RANGES) {
    const chip = el("button", { className: "chip" + (anRange === days && anDaysLoaded <= days ? " on" : ""),
      type: "button", textContent: label });
    chip.onclick = () => {
      anRange = days;
      anSessions = []; anNextBefore = null; anDaysLoaded = 0;
      anTokenSection(root);
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

  // Session history is grouped by local day, newest first.
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
    for (const c of cards) dayCard.append(anSessionCard(c));
    list.append(dayCard);
  }
  if (anNextBefore) {
    const more = el("button", { className: "act", type: "button", textContent: "More ↓ (7 more days)" });
    more.onclick = async () => {
      more.disabled = true;
      try { await anLoadPage(7); anTokenSection(root); }
      catch (e) { toast("error: " + e.message); more.disabled = false; }
    };
    list.append(el("div", { className: "an-more" }, more));
  }
}

// ── Provider Quota — harness quota (spec doc #57, superseding #49) ────────────
// Token Analytics answers *what did we spend*; this answers *how much is left*.
// ONE CARD PER PROVIDER, showing that provider's most recent reading and the
// age of the reading. Nothing about who is signed in — decision #75.
//
// Three rules carry the whole section, and all three are about not lying:
//
// 1. COLOUR IS THRESHOLD-DRIVEN AND PROVIDER-BLIND. It is computed from
//    used_percent alone, never from a provider's own severity field: anthropic
//    sends severity=normal at 22%, openai limit_reached=true at 100%, moonshot
//    nothing at all. Three vocabularies would be three colour rules on one page.
// 2. A MISSING NUMBER IS NEVER DRAWN AS A ZERO. used_percent NULL renders
//    "n/a" with no bar — a window whose percent could not be derived, or a
//    provider that reports counts against limit_value 0. A 0% meter reads as
//    measured, and "we did not get a number" is not 0%.
// 3. THE CARD NEVER MAKES A CLAIM ABOUT THE OPERATOR'S SESSION. It has no
//    label, no account id, no plan and no sign-in language, so the two defects
//    that shipped as false claims — a 403 rendered "signed out — last known"
//    while the operator was actively using Codex (#196), and a lapsed 15-minute
//    Kimi token rendered "no account identified" (#197) — cannot be expressed
//    here at all. A failed probe leaves the last-known figures standing with
//    their age, and says nothing else.
//
// The per-provider status still comes from the response's status field, never
// inferred from whether windows arrived: no windows is both "nothing
// configured" and "the probe failed", and the operator has to be able to tell
// those apart.
const AN_PROVIDER_LABEL = { anthropic: "Claude", openai: "Codex", moonshot: "Kimi" };
// Where the operator goes to manage the account itself. This panel deliberately
// stops at "how much is left" — the FnB's ruling that retired account identity
// rests on the provider's own page being one click away, so the link is part of
// the design and not a convenience. Each URL is taken from the harness CLI's own
// binary rather than guessed; Kimi links to its Code usage console.
const AN_PROVIDER_USAGE_URL = {
  anthropic: "https://claude.ai/settings/usage",
  openai: "https://chatgpt.com/codex/settings/usage",
  moonshot: "https://www.kimi.com/code/console",
};
// Display order for a card's windows. Unrecognized kinds sort last rather than
// being dropped — the probe stores a window it could not map under its raw
// duration precisely so the panel still shows it.
const AN_WINDOW_ORDER = ["session", "five_hour", "weekly", "weekly_scoped", "short"];
const AN_WINDOW_LABEL = {
  session: "Session", five_hour: "5-hour", weekly: "Weekly",
  weekly_scoped: "Weekly · scoped", short: "Short",
};
const anWindowRank = (w) => {
  const i = AN_WINDOW_ORDER.indexOf(w.window_kind);
  return i < 0 ? AN_WINDOW_ORDER.length : i;
};
const anWindowName = (w) => (AN_WINDOW_LABEL[w.window_kind] || w.window_kind)
  + (w.scope ? " · " + w.scope : "");

// below 80 normal · 80+ amber · 95+ red. NULL has no class: an absent number is
// not a low one, so it gets no colour rather than the reassuring one.
const anQuotaClass = (pct) => pct == null ? "" : pct >= 95 ? " red" : pct >= 80 ? " amber" : "";
const anPct = (pct) => pct == null ? "n/a" : Math.round(pct * 10) / 10 + "%";

function anDuration(ms) {
  const mins = Math.round(ms / 60000);
  if (mins < 1) return "<1m";
  if (mins < 60) return mins + "m";
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + "h" + (mins % 60 ? " " + (mins % 60) + "m" : "");
  const days = Math.floor(hrs / 24);
  return days + "d" + (hrs % 24 ? " " + (hrs % 24) + "h" : "");
}
// Reset renders as a countdown, not a timestamp — "in 3h 12m" answers the
// question the operator actually has. A reset already past reads "due" rather
// than a negative duration; an absent or unparseable one reads "—", never now.
function anCountdown(iso) {
  const t = iso ? new Date(iso).getTime() : NaN;
  if (!Number.isFinite(t)) return "—";
  const ms = t - Date.now();
  return ms <= 0 ? "due" : "in " + anDuration(ms);
}
function anAge(iso) {
  const t = iso ? new Date(iso).getTime() : NaN;
  if (!Number.isFinite(t)) return "—";
  const ms = Date.now() - t;
  return ms <= 0 ? "just now" : anDuration(ms) + " ago";
}

function anWindowRow(w) {
  const pct = w.used_percent;
  const row = el("div", { className: "an-win" });
  row.append(el("div", { className: "an-win-head" },
    el("span", { className: "an-win-name" }, anWindowName(w)),
    el("span", { className: "an-win-pct" + anQuotaClass(pct) }, anPct(pct))));
  const meter = el("div", { className: "an-meter" });
  if (pct != null) {
    // Clamped: a provider reporting over 100 fills the track, it does not
    // overflow it. NULL draws no fill at all — an empty track is honest.
    const fill = el("div", { className: "an-meter-fill" + anQuotaClass(pct) });
    fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
    meter.append(fill);
  }
  row.append(meter);
  const meta = [];
  if (w.used != null || w.limit_value != null)
    meta.push(["used", (w.used == null ? "—" : fmt(w.used))
      + " / " + (w.limit_value == null ? "—" : fmt(w.limit_value))]);
  meta.push(["resets", anCountdown(w.resets_at)]);
  if (w.status && w.status !== "ok") meta.push(["status", w.status]);
  const line = statRow(meta);
  if (w.resets_at) line.title = "resets " + w.resets_at;
  row.append(line);
  return row;
}

// The status line a card carries under its heading. It describes THE PROBE, never
// the operator: `unauth` says the token we hold is not usable, which is a fact
// about a credential file, and stops there. The words "signed out" and "no
// account identified" are gone from this section on purpose — both were
// rendered while the operator was signed in and working (#196, #197).
const AN_STATUS_NOTE = {
  na: "no readable credential file",
  unauth: "token not usable",
  error: "probe failed",
};

function anProviderCard(prov) {
  const status = prov.status;
  const card = el("div", { className: "card an-acct" });

  // No muted styling and no dimming, for any status. Muting existed to mark a
  // card as "not the current account", a distinction a provider-level panel
  // does not have — and a dimmed card reads as a lesser truth, when in fact the
  // numbers on a degraded card are exactly as real as the ones on a fresh card.
  // The AGE is what distinguishes them, and it is right there in the footer.
  const head = el("div", { className: "an-acct-head" });
  head.append(el("h2", {}, AN_PROVIDER_LABEL[prov.provider] || prov.provider));
  head.append(el("span", { className: "pill" }, prov.provider));
  if (status && status !== "ok") {
    const note = AN_STATUS_NOTE[status] || status;
    const tokenRefreshes = status === "unauth";
    const badge = el("span", { className: "pill " + (tokenRefreshes ? "info" : "warn") },
      note + (!tokenRefreshes && prov.detail ? " · " + prov.detail : ""));
    if (tokenRefreshes) {
      const provider = AN_PROVIDER_LABEL[prov.provider] || prov.provider;
      badge.title = "Refreshes automatically the next time you use " + provider + "."
        + (prov.detail ? " Last probe: " + prov.detail : "");
    }
    head.append(badge);
  }
  card.append(head);

  const wins = [...(prov.windows || [])].sort((a, b) => anWindowRank(a) - anWindowRank(b));
  // "No reading yet" is not the same sentence as "no windows reported", and the
  // difference is the operator's next move. A provider that has never returned
  // anything has nothing to show; one that returned an intact envelope carrying
  // zero windows is genuinely idle and that IS its reading.
  //
  // THE SIGNAL IS THE STATUS, NOT captured_at, and the distinction is the whole
  // reason this branch exists at all. captured_at is derived from window rows,
  // so a card with no windows never has one — reading it here asked a question
  // whose answer was fixed, and the idle sentence could not be reached by any
  // response the API can emit (L-614-2). `ok` with zero windows is the probe
  // saying it got an intact answer and there was nothing in it; any other
  // status with zero windows means nothing has ever been read, and the pill
  // above already says why.
  if (!wins.length)
    card.append(el("div", { className: "muted" },
      prov.status === "ok" ? "no windows reported" : "no reading yet"));
  for (const w of wins) card.append(anWindowRow(w));

  const foot = el("div", { className: "an-acct-foot" });
  // THE AGE IS NEVER OMITTED AND NEVER SOFTENED. It is the whole of the
  // empty-state rule: a probe that cannot get fresh numbers leaves the last
  // known ones standing, and the only thing that keeps that honest is saying
  // how old they are. A card with no age would present stale figures as fresh.
  if (prov.captured_at)
    foot.append(el("span", { className: "muted" }, "as of " + anAge(prov.captured_at)));
  // NO PER-CARD REFRESH. There was one, and it re-probed all three providers —
  // one probe run is the only thing the route can do. Per-card refresh made
  // sense under the ACCOUNT model, where cards differed in whether they could
  // be refreshed at all; provider cards do not differ that way, so three
  // buttons doing one thing were three labels under-describing it. The
  // section's own "refresh all" says what actually happens and is one control
  // instead of four.
  const actions = el("div", { className: "an-acct-actions" });
  const usageUrl = AN_PROVIDER_USAGE_URL[prov.provider];
  if (usageUrl)
    actions.append(el("a", { className: "act", textContent: "usage page ↗",
      href: usageUrl, target: "_blank", rel: "noopener noreferrer" }));
  foot.append(actions);
  card.append(foot);
  return card;
}

function anDrawQuota(root, d) {
  root.replaceChildren();
  const providers = d.providers || [];

  const head = el("div", { className: "an-acct-bar" });
  head.append(el("span", { className: "muted" }, "most recent reading per provider"));
  const probeAll = el("button", { className: "act", type: "button", textContent: "refresh all ⟳" });
  probeAll.onclick = () => anProbeNow(root, probeAll);
  head.append(probeAll);
  root.append(head);

  if (!providers.length) {
    root.append(el("div", { className: "card muted" }, "No probe has run yet."));
    return;
  }

  // EVERY provider gets a card, including one with no credential file and one
  // that has never been probed. Nothing is filtered out: hiding a card is how a
  // panel stops lying and starts saying nothing, and the operator cannot tell
  // "not configured" from "not readable" from a card that is not there.
  for (const prov of providers) root.append(anProviderCard(prov));
}

// The refresh control — POSTs the probe route, which bypasses the 60s TTL. The
// redraw replaces the button along with everything else, so it is only
// re-enabled on the failure path. It is never disabled by a judgement about
// whether a probe can succeed: the old panel made that judgement from the
// registry's idea of who was signed in and was wrong in exactly the case the
// operator most wants it — a lapsed Kimi token that a re-probe fixes the moment
// they boot the harness.
async function anProbeNow(root, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "probing…";
  try {
    anDrawQuota(root, await api("/analytics/quota/probe", "POST"));
  } catch (e) {
    toast("probe error: " + e.message);
    btn.disabled = false;
    btn.textContent = label;
  }
}

// GET is the arrival probe: the route probes for itself when its last attempt
// has aged past the TTL, so the section fires exactly one probe per minute of
// toggling and the client asks for nothing extra.
async function anQuotaSection(root) {
  root.replaceChildren(el("div", { className: "muted" }, "probing providers…"));
  try {
    anDrawQuota(root, await api("/analytics/quota"));
  } catch (e) {
    root.replaceChildren(el("div", { className: "card" }, "error: " + e.message));
  }
}

// The Analytics tab carries its sub-view in the hash, the convention the
// roadmap tab already uses for #roadmap / #roadmap-flow: #analytics = Token
// Analytics, #analytics-quota = Provider Quota. Both keep `analytics` as the
// active nav tab; routeFromHash sets anView and re-renders.
//
// The hash moved with the section's name. #analytics-accounts described a
// per-account panel that no longer exists, and an operator who lands on a URL
// naming a surface they cannot find is exactly the confusion the rename exists
// to prevent — the old hash simply falls through to Token Analytics.
//
// Each section renders into its OWN node, so its replaceChildren() cannot wipe
// the toggle above it — and so arriving at one section never runs the other's
// work: the token sweep and the account probe both fire on entry, and firing
// both would mean three third-party calls every time someone reads their spend.
async function renderAnalytics(root) {
  const seg = el("div", { className: "filters seg an-view" });
  for (const [mode, label] of [["tokens", "Token Analytics"], ["quota", "Provider Quota"]]) {
    const b = el("button", { className: "chip" + (anView === mode ? " on" : ""),
      type: "button", textContent: label });
    b.onclick = () => { location.hash = mode === "quota" ? "analytics-quota" : "analytics"; };
    seg.append(b);
  }
  const body = el("div", {});
  root.replaceChildren(el("div", { className: "an-head" }, seg), body);
  return anView === "quota" ? anQuotaSection(body) : anTokenSection(body);
}

// ── Interface / browser-native conversations ────────────────────────────────
// A browser conversation owns the shell's single session slot until ended. It
// uses the normal CLI preparation path server-side, but deliberately has no
// terminal, tmux controls, or live hand-off between browser and CLI.
const CHAT_HARNESSES = ["opencode", "claude", "codex", "kimi"];
const CHAT_FLAVOR_ORDER = [
  "cartographer", "admin", "planner", "dev", "reviewer", "devops",
];
const CHAT_CONFIGURE_ROUTE = "configure";
const CHAT_HISTORY_POLL_MS = 2000;
const CHAT_MODES = ["chat", "diff"];
let chatRouteShell = "";
let chatRouteConversation = "";
let chatRouteMode = "chat";
let chatSource = null;
let chatHistoryPollTimer = null;
let chatRenderGeneration = 0;
let chatPendingSend = null;
let chatModeController = null;
let chatReviewCleanup = null;
let chatConfiguration = null;
let chatConfigurationPromise = null;

function chatLoadConfiguration() {
  if (chatConfiguration) return Promise.resolve(chatConfiguration);
  if (chatConfigurationPromise) return chatConfigurationPromise;
  const request = Promise.all([
    api("/flavor-defaults"),
    api("/models"),
  ]).then(([defaults, catalog]) => {
    chatConfiguration = { defaults, catalog };
    return chatConfiguration;
  });
  chatConfigurationPromise = request;
  request.catch(() => {
    if (chatConfigurationPromise === request) chatConfigurationPromise = null;
  });
  return request;
}

function chatStopStream() {
  if (chatSource) chatSource.close();
  chatSource = null;
}

function chatStopHistoryPoll() {
  if (chatHistoryPollTimer) clearInterval(chatHistoryPollTimer);
  chatHistoryPollTimer = null;
}

function chatStopReview() {
  if (chatReviewCleanup) chatReviewCleanup();
  chatReviewCleanup = null;
  document.body.classList.remove("chat-diff-view");
}

function chatHash(shortname, conversationId = "") {
  return `interface/${encodeURIComponent(shortname)}`
    + (conversationId ? `/${encodeURIComponent(conversationId)}` : "");
}

function chatModeHash(shortname, conversationId, mode = "chat") {
  return chatHash(shortname, conversationId)
    + (mode === "diff" ? "/diff" : "");
}

function chatModelLabel(conversation) {
  return conversation.route?.model || "harness default";
}

function chatStartedLabel(conversation) {
  if (!conversation.created_at) return "Start time unavailable";
  const raw = String(conversation.created_at);
  const timestamp = /(?:Z|[+-]\d\d:\d\d)$/.test(raw) ? raw : `${raw}Z`;
  const started = new Date(timestamp);
  if (Number.isNaN(started.getTime())) return raw;
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(started);
}

function chatShellLabel(conversation) {
  const shell = conversation?.shell || {};
  return [shell.display_name, shell.shortname].filter(Boolean).join(" | ") || "Shell";
}

function chatHeaderLabel(conversation) {
  return [
    conversation.shell?.shortname,
    chatModelLabel(conversation),
    conversation.route?.harness,
  ].filter(Boolean).join(" | ");
}

function chatWorkingDots() {
  return el("span", { className: "chat-working-dots", ariaHidden: "true" },
    el("span", {}, "."),
    el("span", {}, "."),
    el("span", {}, "."));
}

function chatStatePill(state) {
  const label = {
    queued: "queued", running: "working", waiting: "waiting",
    error: "failed", idle: "idle", closed: "closed",
  }[state] || state;
  const pill = el("span", {
    className: `chat-state state-${state || "idle"}`,
  });
  if (state !== "running") {
    pill.textContent = label;
    return pill;
  }
  pill.append(label, chatWorkingDots());
  return pill;
}

function chatWorkingIndicator() {
  const indicator = el("div", {
    className: "chat-working-indicator",
    role: "status",
    ariaLabel: "Working",
  });
  indicator.append("<Working>", chatWorkingDots());
  return indicator;
}

const CHAT_SHELL_STATE_CLASSES = [
  "active-chat", "state-idle", "state-queued", "state-running",
  "state-waiting", "state-error",
];

function chatPaintShellState(button, state) {
  button.classList.remove(...CHAT_SHELL_STATE_CLASSES);
  if (!["idle", "queued", "running", "waiting", "error"].includes(state)) return;
  button.classList.add("active-chat", `state-${state}`);
}

function chatPaintStar(button, starred) {
  button.textContent = starred ? "★" : "☆";
  button.classList.toggle("starred", starred);
  button.title = starred ? "Unstar chat" : "Star chat";
  button.setAttribute("aria-label", button.title);
  button.setAttribute("aria-pressed", String(starred));
}

function chatPaintHistoryItem(item, conversation) {
  item.conversation = conversation;
  item.context.textContent =
    `${chatStartedLabel(conversation)} | ${chatModelLabel(conversation)}`;
  item.name.textContent = chatConversationName(conversation);
  const nextState = conversation.state || "idle";
  if (!item.state.classList.contains(`state-${nextState}`)) {
    const state = chatStatePill(nextState);
    item.state.replaceWith(state);
    item.state = state;
  }
  chatPaintStar(item.star, Boolean(conversation.starred));
}

function chatQueuedCount(messages) {
  return messages.filter(
    (message) => message.message_kind !== "control"
      && ["accepted", "queued"].includes(message.state)
  ).length;
}

function chatConversationName(conversation) {
  return conversation.title || "Untitled chat";
}

async function chatCloseForSwitch(conversation) {
  if (!conversation) return true;
  if (conversation.scope === "sprint") return true;
  try {
    const latest = await chatApi(
      `/conversations/${conversation.conversation_id}`,
    );
    if (latest.state === "closed") return true;
    if (!["idle", "waiting", "error"].includes(latest.state)) {
      toast("Finish the current turn and queued messages before switching chats.");
      return false;
    }
    const closed = await chatApi(
      `/conversations/${conversation.conversation_id}`,
      "PATCH",
      { version: latest.version, state: "closed" },
    );
    Object.assign(conversation, closed);
    return true;
  } catch (error) {
    toast(`${error.code}: ${error.message}`);
    return false;
  }
}

function chatBubble(kind, body, meta = "", conversation = null) {
  const bubble = el("article", { className: `chat-bubble chat-${kind}` });
  bubble.append(el("div", { className: "chat-who" },
    kind === "user" ? "You"
      : kind === "assistant" ? chatShellLabel(conversation)
      : "Activity"));
  const content = kind === "activity"
    ? el("div", { className: "chat-activity-text" }, body)
    : mdBlock(body);
  if (kind === "assistant") content.classList.add("chat-assistant-body");
  bubble.append(content);
  if (meta) bubble.append(el("div", { className: "chat-meta" }, meta));
  return bubble;
}

function chatTranscriptAtBottom(transcript) {
  return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight <= 32;
}

async function chatRefreshConversation(conversationId, generation, onUpdate) {
  try {
    const [next, messagePage] = await Promise.all([
      chatApi(`/conversations/${conversationId}`),
      chatApi(`/conversations/${conversationId}/messages?limit=100`),
    ]);
    if (generation === chatRenderGeneration) onUpdate(next, messagePage.items);
  } catch { /* SSE remains authoritative enough to keep the open view usable. */ }
}

function chatOpenStream(
  conversationId, generation, afterSequence, onEvent, onState,
) {
  chatStopStream();
  const source = new EventSource(
    `/api/conversations/${conversationId}/events?after=${afterSequence}`);
  chatSource = source;
  source.onopen = () => onState("connected");
  source.onerror = () => onState("reconnecting");
  const types = [
    "conversation.created", "conversation.updated", "conversation.renamed",
    "conversation.close.requested", "conversation.closed",
    "message.accepted", "session.started", "run.started",
    "assistant.delta", "tool.started", "tool.completed", "permission.requested",
    "input.requested", "usage", "run.completed", "run.failed",
    "run.interrupt.requested", "run.interrupted", "run.unknown",
  ];
  for (const type of types) {
    source.addEventListener(type, (raw) => {
      if (generation !== chatRenderGeneration) return;
      try { onEvent(JSON.parse(raw.data)); } catch { /* malformed frames reconnect */ }
    });
  }
  return source;
}

function chatModelOptions(select, catalog, harness, defaultModel) {
  select.replaceChildren();
  const models = catalog.harnesses?.[harness]?.models || [];
  const available = models.filter((model) => model.availability === "available");
  const connectedDefault = Boolean(
    defaultModel && available.some((model) => model.id === defaultModel));
  if (harness !== "opencode" || connectedDefault) {
    select.append(el("option", {
      value: "",
      textContent: defaultModel
        ? `Use shell default — ${defaultModel}`
        : "Use harness default",
    }));
  }
  for (const model of available) {
    select.append(el("option", { value: model.id, textContent: model.id }));
  }
  if (!select.options.length) {
    select.append(el("option", {
      value: "",
      textContent: "No connected provider models available",
      disabled: true,
      selected: true,
    }));
  }
  return harness !== "opencode" || available.length > 0;
}

function chatCreateConversation(shell, fields = {}) {
  return chatApi(
    "/conversations",
    "POST",
    { shell_id: shell.shell_id, ...fields },
    requestKey(),
  );
}

async function reviewObservationApi(path, { method = "GET", key = "" } = {}) {
  const headers = key ? { "Idempotency-Key": key } : {};
  let response;
  try {
    response = await fetch("/api" + path, { method, headers });
  } catch (cause) {
    const error = new Error("Diff observation could not be reached.");
    error.code = "REVIEW_TARGET_UNAVAILABLE";
    error.cause = cause;
    throw error;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.error || {};
    const error = new Error(detail.message || response.statusText);
    error.code = detail.code || "REVIEW_TARGET_UNAVAILABLE";
    error.details = detail.details || {};
    error.status = response.status;
    throw error;
  }
  return data;
}

function reviewObservedLabel(value) {
  if (!value) return "observed just now";
  const parsed = new Date(/(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(parsed.getTime())
    ? `observed ${value}`
    : `observed ${parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

function reviewTypedState(title, detail = "", tone = "") {
  const state = el("div", {
    className: `review-typed-state${tone ? ` ${tone}` : ""}`,
  }, el("strong", {}, title));
  if (detail) state.append(el("span", {}, detail));
  return state;
}

function reviewPatchRows(text) {
  const patch = el("div", { className: "review-patch" });
  const rows = [];
  let oldLine = null;
  let newLine = null;
  for (const line of String(text || "").split("\n")) {
    let kind = "context";
    let oldNumber = "";
    let newNumber = "";
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) {
      kind = "hunk";
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
    } else if (line.startsWith("diff --git ") || line.startsWith("index ")
        || line.startsWith("--- ") || line.startsWith("+++ ")) {
      kind = "header";
    } else if (line.startsWith("+")) {
      kind = "add";
      newNumber = newLine ?? "";
      if (newLine !== null) newLine += 1;
    } else if (line.startsWith("-")) {
      kind = "delete";
      oldNumber = oldLine ?? "";
      if (oldLine !== null) oldLine += 1;
    } else if (line.startsWith("\\")) {
      kind = "notice";
    } else {
      oldNumber = oldLine ?? "";
      newNumber = newLine ?? "";
      if (oldLine !== null) oldLine += 1;
      if (newLine !== null) newLine += 1;
    }
    const row = el("div", { className: `review-line review-line-${kind}` });
    row.append(
      el("span", { className: "review-line-number" }, oldNumber),
      el("span", { className: "review-line-number" }, newNumber),
      el("span", { className: "review-line-code" }, line),
    );
    patch.append(row);
    rows.push({ kind, row });
  }

  const changeBlocks = [];
  for (const entry of rows) {
    if (entry.kind !== "add" && entry.kind !== "delete") continue;
    const previous = changeBlocks.at(-1);
    if (!previous || previous.last !== entry.row.previousElementSibling) {
      changeBlocks.push({ first: entry.row, last: entry.row });
    } else {
      previous.last = entry.row;
    }
  }
  const scrollToChange = (index) => {
    const target = changeBlocks[index]?.first;
    const scroller = target?.closest(".review-patch-wrap");
    if (!target || !scroller) return;
    const targetBox = target.getBoundingClientRect();
    const scrollerBox = scroller.getBoundingClientRect();
    const top = scroller.scrollTop + targetBox.top - scrollerBox.top
      - (scroller.clientHeight - targetBox.height) / 2;
    scroller.scrollTo({
      top: Math.max(0, top),
      left: scroller.scrollLeft,
      behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches
        ? "auto" : "smooth",
    });
  };
  const changeStep = (direction, targetIndex, customLabel = "") => {
    const label = customLabel
      || (direction === "previous" ? "Previous change" : "Next change");
    const button = el("button", {
      type: "button",
      className: `review-change-step review-change-step-${direction}`,
      ariaLabel: label,
      title: label,
    });
    button.onclick = () => scrollToChange(targetIndex);
    return button;
  };
  if (changeBlocks.length && changeBlocks[0].first !== rows[0]?.row) {
    const firstChange = changeStep("next", 0, "Jump to first change");
    firstChange.classList.add("review-change-step-first");
    rows[0].row.append(firstChange);
  }
  changeBlocks.forEach((block, index) => {
    block.first.classList.add("review-change-first");
    block.last.classList.add("review-change-last");
    if (index > 0) block.first.append(changeStep("previous", index - 1));
    if (index < changeBlocks.length - 1) {
      block.last.append(changeStep("next", index + 1));
    }
  });
  return patch;
}

function reviewFileTree(files, selectedPath, viewed, onSelect) {
  const tree = { directories: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let node = tree;
    for (const part of parts.slice(0, -1)) {
      if (!node.directories.has(part))
        node.directories.set(part, { directories: new Map(), files: [] });
      node = node.directories.get(part);
    }
    node.files.push(file);
  }
  const renderNode = (node, depth = 0) => {
    const fragment = document.createDocumentFragment();
    for (const [name, child] of [...node.directories].sort(([a], [b]) => a.localeCompare(b))) {
      fragment.append(el("div", {
        className: "review-tree-directory",
        style: `--review-depth:${depth}`,
      }, el("span", { ariaHidden: "true" }, "▾"), name));
      fragment.append(renderNode(child, depth + 1));
    }
    for (const file of [...node.files].sort((a, b) => a.path.localeCompare(b.path))) {
      const status = file.conflict ? "!" : file.status === "untracked" ? "?"
        : file.status?.slice(0, 1).toUpperCase() || "M";
      const button = el("button", {
        className: "review-file-row"
          + (file.path === selectedPath ? " selected" : "")
          + (viewed.has(file.path) ? " viewed" : ""),
        type: "button",
        style: `--review-depth:${depth}`,
        title: file.old_path ? `${file.old_path} → ${file.path}` : file.path,
      });
      const fileFacts = [
        file.binary ? "binary" : "",
        file.submodule ? "submodule" : "",
        file.oversized ? "large" : "",
        file.generated ? "generated" : "",
      ].filter(Boolean);
      button.append(
        el("span", { className: `review-file-status status-${file.status}` }, status),
        el("span", { className: "review-file-path" }, file.path.split("/").at(-1)),
        el("span", { className: "review-file-counts" },
          fileFacts.length ? fileFacts.join(" · ")
            : [file.additions != null ? `+${file.additions}` : "",
              file.deletions != null ? `−${file.deletions}` : ""]
              .filter(Boolean).join(" ")),
      );
      button.onclick = () => onSelect(file);
      fragment.append(button);
    }
    return fragment;
  };
  const host = el("div", { className: "review-file-tree" });
  host.append(renderNode(tree));
  return host;
}

function chatReviewWorkspace(host, conversation) {
  const state = {
    mode: "chat",
    snapshot: null,
    loaded: false,
    loading: false,
    refreshInFlight: false,
    requestGeneration: 0,
    group: "changes",
    section: "dirty",
    selectedPath: "",
    pathFilter: "",
    statusFilter: "",
    patch: null,
    shellFile: null,
    contentLoading: false,
    contentError: null,
    error: null,
    refreshWarning: "",
  };
  let refreshButton = null;

  const sectionFiles = (snapshot = state.snapshot) => {
    if (!snapshot || state.group !== "changes") return [];
    if (state.section === "dirty") return snapshot.changes.dirty || [];
    if (state.section === "branch") return snapshot.changes.branch || [];
    return [];
  };
  const shellFiles = (snapshot = state.snapshot) => snapshot?.shell_files || [];
  const itemPath = (item) => state.group === "shell"
    ? (item.paths || []).join("\n")
    : item.path;
  const selectedItem = () => {
    const items = state.group === "shell" ? shellFiles() : sectionFiles();
    return items.find((item) => itemPath(item) === state.selectedPath) || null;
  };
  const visibleFiles = () => sectionFiles().filter((file) => {
    if (state.pathFilter
      && !file.path.toLowerCase().includes(state.pathFilter.toLowerCase())) return false;
    if (state.statusFilter && file.status !== state.statusFilter) return false;
    return true;
  });
  const capturePosition = () => {
    const navigator = $(".review-file-tree", host);
    const patch = $(".review-patch-wrap", host);
    return {
      navigatorTop: navigator?.scrollTop || 0,
      navigatorLeft: navigator?.scrollLeft || 0,
      patchTop: patch?.scrollTop || 0,
      patchLeft: patch?.scrollLeft || 0,
    };
  };
  const restorePosition = (position, preservePatch) => queueMicrotask(() => {
    const navigator = $(".review-file-tree", host);
    if (navigator) {
      navigator.scrollTop = position?.navigatorTop || 0;
      navigator.scrollLeft = position?.navigatorLeft || 0;
    }
    const patch = $(".review-patch-wrap", host);
    if (patch && preservePatch) {
      patch.scrollTop = position?.patchTop || 0;
      patch.scrollLeft = position?.patchLeft || 0;
    }
  });
  const typedError = (error) => reviewTypedState(
    error?.code || "REVIEW_TARGET_UNAVAILABLE",
    error?.message || "Diff is unavailable.",
    "error",
  );
  const tabButton = (value, label, active, onclick) => {
    const button = el("button", {
      type: "button",
      role: "tab",
      className: active === value ? "active" : "",
      ariaSelected: String(active === value),
      textContent: label,
    });
    button.onclick = onclick;
    return button;
  };

  const paint = () => {
    if (!state.loaded && state.loading) {
      host.replaceChildren(reviewTypedState(
        "Observing current worktree…",
        "Fetching origin/main once and reading the selected shell.",
      ));
      return;
    }
    if (!state.snapshot) {
      host.replaceChildren(state.error ? typedError(state.error) : reviewTypedState(
        "Diff unavailable",
        "Select Refresh Diff to try the current shell again.",
      ));
      return;
    }
    const snapshot = state.snapshot;
    const status = snapshot.status || {};
    const facts = [
      status.branch || `detached ${String(status.head_sha || "").slice(0, 8)}`,
      `${status.dirty_count || 0} dirty`,
      `${status.ahead_count || 0} ahead`,
    ];
    if (status.behind) facts.push(`${status.behind} behind origin/main`);
    const warning = state.refreshWarning
      || (!snapshot.fetch?.fresh && snapshot.fetch?.base_stale
        ? "Fetch failed; this observation uses stale origin/main."
        : !status.base_available
          ? "Remote main unavailable; only Dirty can be inspected."
          : "");
    refreshButton = el("button", {
      type: "button",
      className: "act review-refresh",
      textContent: state.refreshInFlight ? "Refreshing…" : "Refresh Diff",
      disabled: state.refreshInFlight,
    });
    refreshButton.onclick = () => observe(true);
    let sectionSwitch = null;
    if (state.group === "changes") {
      sectionSwitch = el("div", {
        className: "review-scope-switch review-change-switch",
        role: "tablist",
        ariaLabel: "Changes view",
      });
      for (const [value, label] of [
        ["dirty", "Dirty"], ["branch", "Branch"], ["commits", "Commits"],
      ]) sectionSwitch.append(tabButton(value, label, state.section, () => {
        if (state.section === value) return;
        state.section = value;
        state.selectedPath = value === "commits" ? "" : sectionFiles()[0]?.path || "";
        state.patch = null;
        state.contentError = null;
        paint();
        loadSelected();
      }));
    }
    const summaryStatus = el("div", { className: "review-status" },
      el("div", { className: "review-lifecycle-block" },
        el("span", { className: "review-lifecycle lifecycle-local" }, "ON DISK"),
        el("span", { className: "review-facts" }, facts.slice(1).join(" · "))),
      el("div", { className: "review-freshness" },
        el("span", {}, reviewObservedLabel(snapshot.observed_at)),
        el("span", {
          className: "warning review-refresh-warning",
          textContent: warning,
          hidden: !warning,
        })));
    const summaryActions = el("div", { className: "review-summary-actions" },
      summaryStatus,
      refreshButton);
    const summary = el("div", { className: "review-summary" },
      el("div", { className: "review-target-control" },
        el("label", { className: "k" }, "Current worktree"),
        el("strong", {}, facts[0])),
      ...(sectionSwitch ? [sectionSwitch] : []),
      summaryActions);
    const groupSwitch = el("div", {
      className: "review-scope-switch review-group-switch",
      role: "tablist",
      ariaLabel: "Diff section",
    },
    tabButton("changes", "Changes", state.group, () => {
      if (state.group === "changes") return;
      state.group = "changes";
      state.section = "dirty";
      state.selectedPath = sectionFiles()[0]?.path || "";
      state.patch = null;
      state.shellFile = null;
      state.contentError = null;
      paint();
      loadSelected();
    }),
    tabButton("shell", "Shell files", state.group, () => {
      if (state.group === "shell") return;
      state.group = "shell";
      state.selectedPath = itemPath(
        shellFiles().find((file) => file.available) || {},
      );
      state.patch = null;
      state.shellFile = null;
      state.contentError = null;
      paint();
      loadSelected();
    }));
    const patchHeader = (title, detail, extraClass = "") => el("div", {
      className: `review-patch-head${extraClass ? ` ${extraClass}` : ""}`,
    },
    el("div", { className: "review-patch-title" },
      el("strong", {}, title),
      el("span", { title: detail }, detail)),
    groupSwitch);
    const workspace = el("div", {
      className: `review-workspace review-workspace-${state.group}`,
    }, summary);

    if (state.group === "changes") {
      if (state.section === "commits") {
        const commits = snapshot.changes.commits || [];
        const body = el("div", { className: "review-commits-pane" },
          patchHeader("Commits", "Ahead commits relative to origin/main", "review-commits-head"));
        if (!commits.length) body.append(reviewTypedState("No visible ahead commits."));
        else {
          const list = el("div", { className: "review-commit-list" });
          for (const commit of commits) list.append(el("article", { className: "review-commit" },
            el("code", {}, commit.short_sha),
            el("div", { className: "review-commit-main" },
              el("strong", {}, commit.subject),
              el("span", {}, `${commit.author} · ${reviewObservedLabel(commit.authored_at)}`))));
          body.append(list);
        }
        workspace.append(body);
        host.replaceChildren(workspace);
        return;
      }
      const pathInput = el("input", {
        type: "search",
        className: "review-path-filter",
        placeholder: "Filter paths",
        value: state.pathFilter,
      });
      pathInput.oninput = () => {
        const start = pathInput.selectionStart;
        state.pathFilter = pathInput.value;
        paint();
        const next = $(".review-path-filter", host);
        next?.focus();
        next?.setSelectionRange(start, start);
      };
      const statuses = [...new Set(sectionFiles().map((file) => file.status))].sort();
      const statusSelect = el("select", {
        className: "review-status-filter",
        ariaLabel: "File status",
      }, el("option", { value: "", textContent: "All statuses" }));
      for (const value of statuses) statusSelect.append(el("option", {
        value,
        selected: value === state.statusFilter,
        textContent: value,
      }));
      statusSelect.onchange = () => {
        state.statusFilter = statusSelect.value;
        paint();
      };
      const navigator = el("aside", { className: "review-navigator" },
        el("div", { className: "review-filters" }, pathInput, statusSelect));
      const files = visibleFiles();
      if (!files.length) navigator.append(reviewTypedState(
        snapshot.no_code_changes ? "No code changes" : `No ${state.section} changes.`,
      ));
      else navigator.append(reviewFileTree(
        files,
        state.selectedPath,
        new Set(),
        (file) => selectItem(file),
      ));
      const selected = selectedItem();
      const patchHead = patchHeader(
        selected?.path || "Patch",
        state.section === "dirty"
          ? "staged, unstaged, conflicted, or untracked relative to HEAD"
          : "merge-base(origin/main, HEAD) through HEAD",
      );
      let patchBody;
      if (state.contentError) patchBody = typedError(state.contentError);
      else if (state.contentLoading) patchBody = reviewTypedState("Loading patch…");
      else if (!selected) patchBody = reviewTypedState("Select a changed file.");
      else if (!state.patch) patchBody = reviewTypedState("Patch unavailable.");
      else if (state.patch.binary) patchBody = reviewTypedState("Binary file", "Binary bytes are never transported.");
      else if (state.patch.truncated) patchBody = reviewTypedState(
        "Patch exceeds review limits",
        state.patch.unavailable_reason || "The bounded patch cannot be displayed.",
        "warning",
      );
      else {
        patchBody = el("div", { className: "review-patch-wrap" });
        patchBody.append(reviewPatchRows(state.patch.patch || ""));
      }
      workspace.append(el("div", { className: "review-body" }, navigator, patchHead,
        el("section", { className: "review-patch-pane" }, patchBody)));
    } else {
      const navigator = el("aside", { className: "review-navigator" });
      const list = el("div", { className: "review-file-tree review-shell-tree" });
      for (const file of shellFiles()) {
        const key = itemPath(file);
        const row = el("button", {
          type: "button",
          className: `review-file-row${key === state.selectedPath ? " selected" : ""}`,
          disabled: !file.available,
          title: (file.paths || []).join("\n"),
        },
        el("span", { className: "review-file-status" }, file.available ? "R" : "!"),
        el("span", { className: "review-file-path" }, file.name),
        el("span", { className: "review-file-counts" },
          file.mismatch ? "mirror mismatch" : (file.paths || []).join(" · ")));
        row.onclick = () => selectItem(file);
        list.append(row);
        if (!file.available) list.append(reviewTypedState(
          file.error || "Unavailable",
          (file.paths || []).join(" · "),
          "warning",
        ));
      }
      navigator.append(list);
      const selected = selectedItem();
      const head = patchHeader(
        selected?.name || "Shell file",
        selected ? (selected.paths || []).join(" · ") : "Read-only exact text",
      );
      let body;
      if (state.contentError) body = typedError(state.contentError);
      else if (state.contentLoading) body = reviewTypedState("Loading shell file…");
      else if (!selected) body = reviewTypedState("Select an available Shell file.");
      else if (!state.shellFile) body = reviewTypedState("Shell file unavailable.");
      else body = el("pre", {
        className: "review-shell-file review-patch-wrap",
        textContent: state.shellFile.body,
      });
      workspace.append(el("div", { className: "review-body" }, navigator, head,
        el("section", { className: "review-patch-pane" }, body)));
    }
    host.replaceChildren(workspace);
  };

  const loadSelected = async ({ position = null, preservePatch = false } = {}) => {
    const selected = selectedItem();
    if (!selected || (state.group === "changes" && state.section === "commits")) {
      paint();
      restorePosition(position, false);
      return;
    }
    const request = ++state.requestGeneration;
    state.contentLoading = true;
    state.contentError = null;
    state.patch = null;
    state.shellFile = null;
    paint();
    try {
      const resource = state.group === "shell" ? "shell-file" : "patch";
      const data = await reviewObservationApi(
        `/review-observations/${state.snapshot.fingerprint}/${resource}`
        + `?file=${encodeURIComponent(selected.file_id)}`,
      );
      if (request !== state.requestGeneration) return;
      if (state.group === "shell") state.shellFile = data;
      else state.patch = data;
    } catch (error) {
      if (request !== state.requestGeneration) return;
      state.contentError = error;
    } finally {
      if (request === state.requestGeneration) {
        state.contentLoading = false;
        paint();
        restorePosition(position, preservePatch && !state.contentError);
      }
    }
  };

  const selectItem = (item) => {
    const nextPath = itemPath(item);
    if (!nextPath || nextPath === state.selectedPath) return;
    state.selectedPath = nextPath;
    state.contentError = null;
    loadSelected();
  };

  const observe = async (manual = false) => {
    if (state.refreshInFlight) return;
    state.refreshInFlight = true;
    state.loading = !state.snapshot;
    state.error = null;
    const position = capturePosition();
    const oldSnapshot = state.snapshot;
    const oldItems = state.group === "shell"
      ? shellFiles(oldSnapshot).filter((file) => file.available)
      : sectionFiles(oldSnapshot);
    const oldIndex = oldItems.findIndex((item) => itemPath(item) === state.selectedPath);
    const oldPath = state.selectedPath;
    if (!oldSnapshot) paint();
    if (refreshButton) {
      refreshButton.disabled = true;
      refreshButton.textContent = "Refreshing…";
    }
    try {
      const next = await reviewObservationApi(
        `/conversations/${conversation.conversation_id}/review-observations`,
        { method: "POST", key: requestKey() },
      );
      state.loaded = true;
      if (manual && oldSnapshot && !next.fetch?.fresh) {
        state.refreshWarning = next.fetch?.error
          ? `Refresh failed: ${next.fetch.error}`
          : "Refresh failed; the current observation is retained.";
        const warning = $(".review-refresh-warning", host);
        if (warning) {
          warning.hidden = false;
          warning.textContent = state.refreshWarning;
        }
        return;
      }
      state.refreshWarning = "";
      if (oldSnapshot && oldSnapshot.fingerprint === next.fingerprint) return;
      state.snapshot = next;
      const nextItems = state.group === "shell"
        ? shellFiles(next).filter((file) => file.available)
        : sectionFiles(next);
      const stillPresent = nextItems.find((item) => itemPath(item) === oldPath);
      const nearest = nextItems[Math.min(Math.max(oldIndex, 0), nextItems.length - 1)];
      state.selectedPath = itemPath(stillPresent || nearest || {});
      state.patch = null;
      state.shellFile = null;
      state.contentError = null;
      paint();
      await loadSelected({ position, preservePatch: Boolean(stillPresent) });
    } catch (error) {
      state.error = error;
      state.loaded = true;
      if (!oldSnapshot) paint();
      else {
        state.refreshWarning = error.message || "Refresh failed.";
        const warning = $(".review-refresh-warning", host);
        if (warning) {
          warning.hidden = false;
          warning.textContent = state.refreshWarning;
        }
      }
    } finally {
      state.loading = false;
      state.refreshInFlight = false;
      if (refreshButton) {
        refreshButton.disabled = false;
        refreshButton.textContent = "Refresh Diff";
      }
    }
  };

  const setMode = (mode) => {
    state.mode = mode;
    if (mode === "diff" && !state.loaded && !state.loading) observe(false);
  };
  const cleanup = () => {
    state.requestGeneration += 1;
  };
  chatReviewCleanup = cleanup;
  return { setMode, cleanup };
}

async function chatRenderNew(host, shell, defaults, catalog) {
  const rows = defaults.flavors?.[shell.flavor] || [];
  const byHarness = Object.fromEntries(rows.map((row) => [row.harness, row]));
  const defaultHarness = rows.find((row) => row.is_default)?.harness;
  const availableHarnesses = CHAT_HARNESSES;
  let harness = availableHarnesses.includes(defaultHarness)
    ? defaultHarness : availableHarnesses[0];
  const form = el("form", { className: "chat-new-form" });
  const harnessSelect = el("select");
  for (const value of availableHarnesses) {
    harnessSelect.append(el("option", {
      value,
      selected: value === harness,
      textContent: value + (value === defaultHarness ? " — shell default" : ""),
    }));
  }
  const modelSelect = el("select");
  const title = el("input", {
    type: "text",
    placeholder: "Optional chat title",
    maxlength: 200,
  });
  const submit = el("button", {
    className: "act primary",
    type: "submit",
    textContent: "Start chat",
  });
  const routeNote = el("div", { className: "chat-route-note" });
  const paintModels = () => {
    harness = harnessSelect.value;
    const ready = chatModelOptions(
      modelSelect, catalog, harness, byHarness[harness]?.model);
    submit.disabled = !ready;
    routeNote.textContent = harness === "opencode"
      ? "OpenCode models come only from providers connected in OpenCode."
      : "Choose an exact installed model, or keep the shell/harness default.";
  };
  harnessSelect.onchange = paintModels;
  paintModels();
  form.append(
    el("div", { className: "chat-new-copy" },
      el("h2", {}, `Start a chat with ${shell.display_name}`),
      el("p", { className: "muted" },
        "This prepares the shell through its normal CLI path, then runs each turn headlessly.")),
    el("label", { className: "k" }, "Harness"), harnessSelect,
    el("label", { className: "k" }, "Model"), modelSelect,
    routeNote,
    el("label", { className: "k" }, "Title"), title,
    submit,
  );
  form.onsubmit = async (event) => {
    event.preventDefault();
    submit.disabled = true;
    submit.textContent = "Starting…";
    const body = {
      harness: harnessSelect.value,
      title: title.value.trim() || null,
    };
    if (modelSelect.value) body.model = modelSelect.value;
    try {
      const conversation = await chatCreateConversation(shell, body);
      location.hash = chatHash(shell.shortname, conversation.conversation_id);
    } catch (error) {
      toast(`${error.code}: ${error.message}`);
      submit.disabled = false;
      submit.textContent = "Start chat";
    }
  };
  host.replaceChildren(form);
}

function chatCreateTranscriptState(snapshot) {
  if (snapshot.projection_version !== 1)
    throw new Error("Unsupported transcript projection.");
  const items = new Map();
  for (const item of snapshot.items || []) {
    if (!item.item_id || items.has(item.item_id))
      throw new Error("Transcript contains a keyed identity conflict.");
    items.set(item.item_id, { ...item });
  }
  return {
    projectionVersion: snapshot.projection_version,
    throughSequence: Number(snapshot.through_sequence || 0),
    lastSequence: Number(snapshot.through_sequence || 0),
    items,
    order: [...items.keys()].sort((left, right) => {
      const a = items.get(left);
      const b = items.get(right);
      return Number(a.order_sequence) - Number(b.order_sequence)
        || left.localeCompare(right);
    }),
    nodes: new Map(),
    dirty: new Set(items.keys()),
    fullBuild: true,
    hiddenDirty: false,
    frame: null,
    truncation: snapshot.truncation || null,
    reconcileError: null,
  };
}

function chatTranscriptItemNode(item, conversation, retry) {
  if (item.kind === "activity")
    return chatBubble("activity", item.label || item.activity_type);
  const node = chatBubble(
    item.kind,
    item.text || "",
    item.kind === "user" ? item.state || "" : "",
    conversation,
  );
  if (item.kind === "user" && item.state === "failed") {
    const button = el("button", {
      className: "chat-retry",
      type: "button",
      textContent: "Retry",
    });
    button.onclick = () => retry(item.text);
    node.append(button);
  }
  return node;
}

function chatUpdateTranscriptNode(node, item, retry) {
  if (item.kind === "assistant") {
    const body = node.querySelector(".chat-assistant-body");
    const rendered = mdBlock(item.text || "");
    body.replaceChildren(...rendered.childNodes);
    return;
  }
  if (item.kind !== "user") return;
  let meta = node.querySelector(".chat-meta");
  if (item.state) {
    if (!meta) {
      meta = el("div", { className: "chat-meta" });
      node.append(meta);
    }
    meta.textContent = item.state;
  }
  let retryButton = node.querySelector(".chat-retry");
  if (item.state === "failed" && !retryButton) {
    retryButton = el("button", {
      className: "chat-retry",
      type: "button",
      textContent: "Retry",
    });
    retryButton.onclick = () => retry(item.text);
    node.append(retryButton);
  } else if (item.state !== "failed" && retryButton) {
    retryButton.remove();
  }
}

function chatTranscriptBanner(state) {
  if (!state.truncation) return null;
  return el(
    "div",
    { className: "chat-transcript-omission", role: "status" },
    "Earlier transcript omitted from this view; durable history was not deleted.",
  );
}

function chatReconcileBanner(state, reconcile) {
  if (!state.reconcileError) return null;
  const retry = el("button", {
    className: "act",
    type: "button",
    textContent: "Retry",
  });
  retry.onclick = reconcile;
  return el(
    "div",
    { className: "chat-transcript-reconcile error" },
    `Transcript reconciliation failed — ${state.reconcileError.message} `,
    retry,
  );
}

function chatFlushTranscript(
  transcript,
  state,
  conversation,
  retry,
  reconcile,
  shouldFollow,
  onPosition,
) {
  const previousTop = transcript.scrollTop;
  const followTail = shouldFollow();
  if (state.fullBuild) {
    state.nodes.clear();
    const nodes = [];
    const banner = chatTranscriptBanner(state);
    const reconcileBanner = chatReconcileBanner(state, reconcile);
    if (banner) nodes.push(banner);
    if (reconcileBanner) nodes.push(reconcileBanner);
    for (const id of state.order) {
      const item = state.items.get(id);
      const node = chatTranscriptItemNode(item, conversation, retry);
      state.nodes.set(id, node);
      nodes.push(node);
    }
    if (!state.order.length) {
      nodes.push(el(
        "div",
        { className: "chat-empty" },
        `Start a conversation with ${conversation.shell.display_name}.`,
      ));
    }
    transcript.replaceChildren(...nodes);
    state.fullBuild = false;
    state.dirty.clear();
  } else {
    if (state.order.length && state.nodes.size === 0)
      transcript.querySelector(".chat-empty")?.remove();
    for (const id of state.order) {
      if (!state.dirty.has(id)) continue;
      const item = state.items.get(id);
      let node = state.nodes.get(id);
      if (!node) {
        node = chatTranscriptItemNode(item, conversation, retry);
        state.nodes.set(id, node);
        const index = state.order.indexOf(id);
        const next = state.order
          .slice(index + 1)
          .map((nextId) => state.nodes.get(nextId))
          .find(Boolean);
        const working = transcript.querySelector(".chat-working-indicator");
        transcript.insertBefore(node, next || working || null);
      } else {
        chatUpdateTranscriptNode(node, item, retry);
      }
    }
    state.dirty.clear();
  }
  const working = transcript.querySelector(".chat-working-indicator");
  if (conversation.state === "running" && !working)
    transcript.append(chatWorkingIndicator());
  else if (conversation.state !== "running" && working)
    working.remove();
  transcript.scrollTop = followTail ? transcript.scrollHeight : previousTop;
  onPosition();
}

async function chatRenderOpen(host, initialConversation, initialSnapshot) {
  const generation = chatRenderGeneration;
  let conversation = initialConversation;
  let transcriptState = chatCreateTranscriptState(initialSnapshot);
  let messages = [...transcriptState.items.values()]
    .filter((item) => item.kind === "user")
    .map((item) => ({
      message_id: item.message_id,
      message_kind: "prompt",
      body: item.text,
      state: item.state,
      created_at: item.created_at,
      completed_at: item.completed_at,
    }));
  conversation = {
    ...conversation,
    version: initialSnapshot.controls.conversation_version,
    state: initialSnapshot.controls.conversation_state,
    close_requested_at: initialSnapshot.controls.close_requested_at,
    active_run_id: initialSnapshot.controls.active_run_id,
  };
  let streamStatus = "connecting";
  let stopRequest = null;
  let reconcilePromise = null;
  let reconcileFailures = 0;
  let currentMode = CHAT_MODES.includes(chatRouteMode) ? chatRouteMode : "chat";

  const header = el("div", { className: "chat-pane-head" });
  const title = el("div", { className: "chat-pane-title" });
  const modeSwitch = el("div", {
    className: "chat-mode-switch",
    role: "tablist",
    ariaLabel: "Conversation mode",
  });
  const chatModeButton = el("button", {
    type: "button",
    role: "tab",
    textContent: "Chat",
  });
  const diffModeButton = el("button", {
    type: "button",
    role: "tab",
    textContent: "Diff",
  });
  modeSwitch.append(chatModeButton, diffModeButton);
  const queueState = el("span", { className: "chat-queue-state", hidden: true });
  const actions = el("div", { className: "chat-actions" });
  const transcriptHost = el("div", { className: "chat-transcript-host" });
  const reviewHost = el("div", { className: "chat-review-host", hidden: true });
  const transcript = el("div", { className: "chat-transcript" });
  const jumpToLatest = el("button", {
    className: "chat-jump-latest",
    type: "button",
    title: "Jump to latest",
    ariaLabel: "Jump to latest message",
    textContent: "↓",
    hidden: true,
  });
  transcriptHost.append(transcript, jumpToLatest);
  let followTranscriptTail = true;
  const updateTranscriptFollow = () => {
    if (currentMode !== "chat") return;
    followTranscriptTail = chatTranscriptAtBottom(transcript);
    jumpToLatest.hidden = followTranscriptTail;
  };
  transcript.onscroll = updateTranscriptFollow;
  jumpToLatest.onclick = () => {
    transcript.scrollTop = transcript.scrollHeight;
    updateTranscriptFollow();
  };
  const composer = el("textarea", {
    className: "chat-composer-input",
    placeholder: "Message this shell…",
    rows: 3,
  });
  const send = el("button", { className: "act primary", type: "button", textContent: "Send" });
  const stop = el("button", {
    className: "act danger chat-stop",
    type: "button",
    textContent: "Stop",
    title: "Interrupt the active turn",
  });
  const headerStop = el("button", {
    className: "act danger chat-stop-header",
    type: "button",
    textContent: "Stop",
    title: "Interrupt the active turn",
    hidden: true,
  });
  const pending = el("div", { className: "chat-pending", hidden: true });
  const composerRow = el("div", { className: "chat-composer" },
    composer, el("div", { className: "chat-compose-actions" }, pending, send, stop));
  const reviewWorkspace = chatReviewWorkspace(reviewHost, conversation);
  const updateStreamStatus = () => {
    const connected = streamStatus === "connected";
    const connectionLabel = connected
      ? "Connected"
      : streamStatus === "reconnecting" ? "Reconnecting" : "Connecting";
    transcriptHost.title = `Connection: ${connectionLabel}`;
    transcriptHost.setAttribute(
      "aria-label",
      `Conversation transcript; connection ${connectionLabel.toLowerCase()}`,
    );
    transcriptHost.classList.toggle("stream-disconnected", !connected);
  };

  const retry = async (text) => {
    composer.value = text;
    composer.focus();
    await submit();
  };
  const flushTranscript = () => chatFlushTranscript(
    transcript,
    transcriptState,
    conversation,
    retry,
    () => reconcileTranscript(true),
    () => followTranscriptTail,
    updateTranscriptFollow,
  );
  const scheduleTranscript = () => {
    if (currentMode !== "chat") {
      transcriptState.hiddenDirty = true;
      return;
    }
    if (transcriptState.frame !== null) return;
    transcriptState.frame = requestAnimationFrame(() => {
      transcriptState.frame = null;
      transcriptState.hiddenDirty = false;
      flushTranscript();
    });
  };
  const installSnapshot = (snapshot) => {
    if (snapshot.conversation_id !== conversation.conversation_id)
      throw new Error("Transcript conversation identity changed.");
    if (transcriptState.frame !== null)
      cancelAnimationFrame(transcriptState.frame);
    transcriptState = chatCreateTranscriptState(snapshot);
    messages = [...transcriptState.items.values()]
      .filter((item) => item.kind === "user")
      .map((item) => ({
        message_id: item.message_id,
        message_kind: "prompt",
        body: item.text,
        state: item.state,
        created_at: item.created_at,
        completed_at: item.completed_at,
      }));
    conversation.version = snapshot.controls.conversation_version;
    conversation.state = snapshot.controls.conversation_state;
    conversation.close_requested_at = snapshot.controls.close_requested_at;
    conversation.active_run_id = snapshot.controls.active_run_id;
  };
  const reconcileTranscript = async (manual = false) => {
    if (manual) reconcileFailures = 0;
    if (reconcilePromise || (!manual && reconcileFailures >= 2))
      return reconcilePromise;
    const request = chatApi(
      `/conversations/${conversation.conversation_id}/transcript`,
    );
    reconcilePromise = request;
    try {
      const snapshot = await request;
      if (generation !== chatRenderGeneration) return;
      installSnapshot(snapshot);
      reconcileFailures = 0;
      transcriptState.reconcileError = null;
      paint();
    } catch (error) {
      reconcileFailures += 1;
      transcriptState.reconcileError = error;
      transcriptState.fullBuild = true;
      if (currentMode === "chat") scheduleTranscript();
    } finally {
      if (reconcilePromise === request) reconcilePromise = null;
    }
    return null;
  };
  const setMode = (mode) => {
    currentMode = CHAT_MODES.includes(mode) ? mode : "chat";
    chatRouteMode = currentMode;
    document.body.classList.toggle("chat-diff-view", currentMode === "diff");
    transcriptHost.hidden = currentMode !== "chat";
    composerRow.hidden = currentMode !== "chat";
    reviewHost.hidden = currentMode !== "diff";
    chatModeButton.classList.toggle("active", currentMode === "chat");
    diffModeButton.classList.toggle("active", currentMode === "diff");
    chatModeButton.setAttribute("aria-selected", String(currentMode === "chat"));
    diffModeButton.setAttribute("aria-selected", String(currentMode === "diff"));
    reviewWorkspace.setMode(currentMode);
    if (currentMode === "chat" && transcriptState.hiddenDirty)
      scheduleTranscript();
  };
  const selectMode = (mode) => {
    if (mode === currentMode) return;
    history.pushState(
      null,
      "",
      `#${chatModeHash(
        conversation.shell.shortname,
        conversation.conversation_id,
        mode,
      )}`,
    );
    setMode(mode);
    paint();
  };
  chatModeButton.onclick = () => selectMode("chat");
  diffModeButton.onclick = () => selectMode("diff");
  const renameConversation = async () => {
    const value = (prompt("Conversation title", conversation.title || "") || "").trim();
    if (!value) return;
    try {
      conversation = await chatApi(`/conversations/${conversation.conversation_id}`,
        "PATCH", { version: conversation.version, title: value });
      paint();
    } catch (error) { toast(`${error.code}: ${error.message}`); refresh(); }
  };
  const paint = () => {
    const rename = el("button", {
      className: "chat-title-button",
      type: "button",
      title: "Rename conversation",
      textContent: chatConversationName(conversation),
    });
    rename.onclick = renameConversation;
    title.replaceChildren(
      el("h2", {}, chatHeaderLabel(conversation)),
      el("div", { className: "chat-conversation-line" },
        el("span", {}, chatStartedLabel(conversation)),
        el("span", { className: "chat-context-separator" }, " | "),
        rename));
    const queued = chatQueuedCount(messages);
    queueState.hidden = queued === 0;
    queueState.textContent = `${queued} queued`;
    updateStreamStatus();
    const closed = conversation.state === "closed";
    const closing = !closed && Boolean(conversation.close_requested_at);
    const sprintManaged = conversation.scope === "sprint";
    composer.disabled = closed || closing;
    send.disabled = closed || closing;
    stop.disabled = conversation.state !== "running" || closing || Boolean(stopRequest);
    stop.textContent = stopRequest ? "Stopping…" : "Stop";
    headerStop.disabled = stop.disabled;
    headerStop.textContent = stop.textContent;
    headerStop.hidden = currentMode !== "diff";
    close.hidden = sprintManaged;
    close.disabled = sprintManaged || closed || closing;
    close.textContent = closing ? "Closing…" : "Close";
    composer.placeholder = closed
      ? "This conversation is closed."
      : closing ? "Stopping work and closing…" : "Message this shell…";
    scheduleTranscript();
  };
  const refresh = () => chatRefreshConversation(
    conversation.conversation_id,
    generation,
    (next, nextMessages) => {
      conversation = next;
      messages = nextMessages;
      for (const message of nextMessages) {
        const item = transcriptState.items.get(
          `message:${message.message_id}`);
        if (!item || item.state === message.state) continue;
        item.state = message.state;
        item.completed_at = message.completed_at;
        transcriptState.dirty.add(item.item_id);
      }
      paint();
    });

  const analytics = el("button", { className: "act", type: "button", textContent: "Analytics" });
  analytics.onclick = () => {
    anFilters.harness = conversation.route.harness || "";
    anFilters.model = conversation.route.model || "";
    location.hash = "analytics";
  };
  const close = el("button", {
    className: "act danger",
    type: "button",
    textContent: "Close",
  });
  close.onclick = async () => {
    if (close.disabled) return;
    close.disabled = true;
    close.textContent = "Closing…";
    try {
      const latest = await chatApi(
        `/conversations/${conversation.conversation_id}`);
      if (latest.state === "closed") {
        conversation = latest;
        paint();
        return;
      }
      conversation = await chatApi(`/conversations/${conversation.conversation_id}`,
        "PATCH", { version: latest.version, state: "closed" });
      paint();
    } catch (error) { toast(`${error.code}: ${error.message}`); refresh(); }
  };
  actions.append(analytics, close);
  actions.insertBefore(headerStop, close);
  header.append(title, queueState, actions);
  header.insertBefore(modeSwitch, queueState);

  async function submit() {
    const text = composer.value.trim();
    if (!text || send.disabled) return;
    if (!chatPendingSend || chatPendingSend.text !== text
        || chatPendingSend.conversationId !== conversation.conversation_id) {
      chatPendingSend = {
        conversationId: conversation.conversation_id,
        text,
        key: requestKey(),
      };
    }
    send.disabled = true;
    pending.hidden = false;
    pending.textContent = "sending…";
    try {
      const result = await chatApi(
        `/conversations/${conversation.conversation_id}/messages`,
        "POST", { text }, chatPendingSend.key);
      if (!messages.some((item) => item.message_id === result.message.message_id))
        messages.push(result.message);
      const userItemId = `message:${result.message.message_id}`;
      if (!transcriptState.items.has(userItemId)) {
        transcriptState.items.set(userItemId, {
          item_id: userItemId,
          kind: "user",
          order_sequence: transcriptState.lastSequence + 1,
          message_id: result.message.message_id,
          run_id: null,
          created_at: result.message.created_at,
          text,
          state: result.message.state,
          completed_at: result.message.completed_at,
          text_truncated: false,
        });
        transcriptState.order.push(userItemId);
        transcriptState.dirty.add(userItemId);
      }
      chatPendingSend = null;
      composer.value = "";
      pending.hidden = true;
      if (conversation.state !== "running") conversation.state = "queued";
      paint();
      refresh();
    } catch (error) {
      pending.hidden = false;
      pending.textContent = `${error.code} — retry keeps this exact send`;
      toast(`${error.code}: ${error.message}`);
    } finally {
      send.disabled = conversation.state === "closed"
        || Boolean(conversation.close_requested_at);
    }
  }
  send.onclick = submit;
  stop.onclick = async () => {
    if (conversation.state !== "running") return;
    if (!stopRequest) stopRequest = { key: requestKey() };
    paint();
    try {
      await chatApi(
        `/conversations/${conversation.conversation_id}/interruptions`,
        "POST", {}, stopRequest.key);
    } catch (error) {
      stopRequest = null;
      toast(`${error.code}: ${error.message}`);
      refresh();
      paint();
    }
  };
  headerStop.onclick = () => stop.click();
  composer.onkeydown = (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };
  host.replaceChildren(header, transcriptHost, reviewHost, composerRow);
  chatModeController = {
    shell: conversation.shell.shortname,
    conversationId: conversation.conversation_id,
    setMode: (mode) => {
      setMode(mode);
      paint();
    },
  };
  setMode(currentMode);
  paint();
  const activityLabel = (event) => {
    const payload = event.payload || {};
    if (event.event_type === "permission.requested")
      return "Waiting for permission";
    if (event.event_type === "input.requested") return "Waiting for input";
    if (event.event_type === "run.interrupted") return "Turn interrupted";
    if (event.event_type === "run.unknown")
      return "Turn outcome could not be proven";
    return payload.error
      ? `Turn failed — ${payload.error}`
      : "Turn failed";
  };
  const reduceEvent = (event) => {
    const sequence = Number(event.sequence);
    if (!Number.isFinite(sequence) || sequence <= transcriptState.lastSequence)
      return;
    if (sequence !== transcriptState.lastSequence + 1) {
      reconcileTranscript();
      return;
    }
    const type = event.event_type;
    const message = messages.find(
      (item) => item.message_id === event.message_id);
    const userItem = event.message_id == null
      ? null
      : transcriptState.items.get(`message:${event.message_id}`);

    if (type === "message.accepted") {
      if (!userItem) {
        reconcileTranscript();
        return;
      }
      userItem.order_sequence = sequence;
      userItem.state = "queued";
      transcriptState.order.sort((left, right) => {
        const a = transcriptState.items.get(left);
        const b = transcriptState.items.get(right);
        return Number(a.order_sequence) - Number(b.order_sequence)
          || left.localeCompare(right);
      });
      transcriptState.dirty.add(userItem.item_id);
    } else if (type === "assistant.delta") {
      if (typeof event.payload?.text !== "string") {
        reconcileTranscript();
        return;
      }
      const runId = event.run_id;
      const itemId = `run:${runId}:assistant`;
      let assistant = transcriptState.items.get(itemId);
      if (assistant && assistant.message_id !== event.message_id) {
        reconcileTranscript();
        return;
      }
      if (!assistant) {
        assistant = {
          item_id: itemId,
          kind: "assistant",
          order_sequence: sequence,
          message_id: event.message_id,
          run_id: runId,
          created_at: event.created_at,
          text: "",
          outcome: null,
          first_sequence: sequence,
          last_sequence: sequence,
          text_truncated: false,
        };
        transcriptState.items.set(itemId, assistant);
        transcriptState.order.push(itemId);
      }
      assistant.text += event.payload?.text || "";
      assistant.last_sequence = sequence;
      transcriptState.dirty.add(itemId);
    } else if ([
      "permission.requested",
      "input.requested",
      "run.failed",
      "run.interrupted",
      "run.unknown",
    ].includes(type)) {
      const itemId = `event:${sequence}`;
      transcriptState.items.set(itemId, {
        item_id: itemId,
        kind: "activity",
        order_sequence: sequence,
        message_id: event.message_id,
        run_id: event.run_id,
        created_at: event.created_at,
        activity_type: type,
        label: activityLabel(event),
        sequence,
      });
      transcriptState.order.push(itemId);
      transcriptState.dirty.add(itemId);
    }

    transcriptState.lastSequence = sequence;
    if (message && type === "run.started") message.state = "running";
    if (message && type === "run.completed") message.state = "completed";
    if (message && ["run.failed", "run.unknown"].includes(type))
      message.state = "failed";
    if (message && type === "run.interrupted") message.state = "cancelled";
    if (userItem && message) {
      userItem.state = message.state;
      userItem.completed_at = message.completed_at;
      if (type !== "assistant.delta")
        transcriptState.dirty.add(userItem.item_id);
    }
    if (type === "message.accepted" && conversation.state !== "running")
      conversation.state = "queued";
    if (type === "run.started") {
      conversation.state = "running";
      conversation.active_run_id = event.run_id;
    }
    if (type === "conversation.updated" || type === "conversation.renamed")
      Object.assign(conversation, event.payload || {});
    if (type === "conversation.close.requested")
      conversation.close_requested_at = event.created_at || true;
    if (["permission.requested", "input.requested"].includes(type))
      conversation.state = "waiting";
    if (["run.completed", "run.interrupted"].includes(type))
      conversation.state = chatQueuedCount(messages) ? "queued" : "idle";
    if (["run.failed", "run.unknown"].includes(type))
      conversation.state = "error";
    if (type === "conversation.closed") conversation.state = "closed";
    if (["run.completed", "run.failed", "run.interrupted", "run.unknown"]
      .includes(type)) {
      stopRequest = null;
      conversation.active_run_id = null;
    }

    if (type === "assistant.delta") {
      scheduleTranscript();
      return;
    }
    paint();
    if (["message.accepted", "run.completed", "run.failed",
         "run.interrupted", "run.unknown",
         "conversation.updated", "conversation.renamed",
         "conversation.close.requested",
         "conversation.closed"].includes(type))
      refresh();
  };
  chatOpenStream(
    conversation.conversation_id,
    generation,
    transcriptState.throughSequence,
    reduceEvent,
    (value) => {
      streamStatus = value;
      updateStreamStatus();
    },
  );
}

async function renderInterface(root) {
  chatStopStream();
  chatStopHistoryPoll();
  chatStopReview();
  chatModeController = null;
  const generation = ++chatRenderGeneration;
  root.replaceChildren(
    el("div", { className: "chat-loading" }, "Loading conversations…"));

  const shellRequest = api("/shells");
  const openRequest = chatApi("/conversations?open=true&limit=100")
    .catch((error) => ({ items: [], next_cursor: null, error }));
  const deepLinked = chatRouteConversation
    && chatRouteConversation !== CHAT_CONFIGURE_ROUTE;
  const detailRequest = deepLinked
    ? chatApi(`/conversations/${chatRouteConversation}`)
      .then((conversation) => ({ conversation }))
      .catch((error) => ({ error }))
    : Promise.resolve({ conversation: null });
  const [{ shells: allShells }, openPage, detailResult] = await Promise.all([
    shellRequest,
    openRequest,
    detailRequest,
  ]);
  if (generation !== chatRenderGeneration) return;

  const shells = allShells;
  if (!shells.length) {
    root.replaceChildren(el("div", { className: "card muted" }, "No shells."));
    return;
  }

  let selectedConversation = detailResult.conversation;
  if (selectedConversation
      && selectedConversation.shell.shortname !== chatRouteShell) {
    chatRouteShell = selectedConversation.shell.shortname;
    history.replaceState(
      null,
      "",
      `#${chatModeHash(
        chatRouteShell,
        selectedConversation.conversation_id,
        chatRouteMode,
      )}`,
    );
  }
  const openConversation = openPage.items.find(
    (item) => item.state !== "closed");
  const shell = selectedConversation?.shell
    ? shells.find(
      (item) => item.shell_id === selectedConversation.shell.shell_id)
    : shells.find((item) => item.shortname === chatRouteShell)
      || (!chatRouteShell && openConversation
        ? shells.find(
          (item) => item.shell_id === openConversation.shell.shell_id)
        : null)
      || shells[0];
  const configuring = chatRouteConversation === CHAT_CONFIGURE_ROUTE;
  if (!selectedConversation && !configuring && !deepLinked) {
    selectedConversation = openPage.items.find(
      (item) => item.shell.shell_id === shell.shell_id
        && item.state !== "closed",
    ) || null;
  }
  const selectedId = selectedConversation?.conversation_id
    || (deepLinked ? chatRouteConversation : "");

  const recentRequest = chatApi(
    `/conversations?shell_id=${shell.shell_id}&starred=false&limit=20`,
  ).catch((error) => ({ items: [], next_cursor: null, error }));
  const recentPage = await recentRequest;
  if (generation !== chatRenderGeneration) return;

  const layout = el("div", { className: "chat-layout" });
  const rail = el("aside", { className: "chat-rail" });
  rail.append(el("div", { className: "chat-rail-title" }, "Shells"));
  const orderedFlavors = [
    ...CHAT_FLAVOR_ORDER.filter(
      (flavor) => shells.some((item) => item.flavor === flavor)),
    ...[...new Set(shells.map((item) => item.flavor || "bespoke"))]
      .filter((flavor) => !CHAT_FLAVOR_ORDER.includes(flavor)).sort(),
  ];
  const orderedShells = orderedFlavors.flatMap((flavor) =>
    shells.filter((item) => (item.flavor || "bespoke") === flavor));
  const shellItems = new Map();
  const openByShell = new Map();
  for (const conversation of openPage.items) {
    if (conversation.state !== "closed")
      openByShell.set(
        conversation.shell.shell_id,
        conversation.state || "idle",
      );
  }
  let previousFlavor = "";
  for (const item of orderedShells) {
    const flavor = item.flavor || "bespoke";
    if (previousFlavor && flavor !== previousFlavor)
      rail.append(
        el("div", { className: "chat-shell-divider", role: "separator" }));
    previousFlavor = flavor;
    const button = el("button", {
      className: "chat-shell"
        + (item.shell_id === shell.shell_id ? " selected" : ""),
      type: "button",
    },
    el("span", { className: "chat-shell-name" }, item.display_name),
    el("span", { className: "chat-shell-shortname" }, item.shortname || ""));
    chatPaintShellState(button, openByShell.get(item.shell_id));
    shellItems.set(item.shell_id, button);
    button.onclick = () => {
      if (item.shell_id === shell.shell_id) return;
      location.hash = chatHash(item.shortname);
    };
    const shellRow = el("div", { className: "chat-shell-row" }, button);
    if (item.sprint) {
      const sprint = item.sprint;
      const pill = el("button", {
        className: "chat-sprint-pill",
        type: "button",
        title: `Sprint ${sprint.sprint_id} · ${sprint.role} · ${sprint.disposition}`,
        ariaLabel: `Enter Sprint ${sprint.sprint_id} ${sprint.role} conversation`,
      },
      el("span", {}, `Sprint ${sprint.sprint_id}`),
      el("span", { className: "chat-sprint-meta" },
        `${sprint.role} · ${sprint.disposition}`));
      pill.onclick = () => {
        location.hash = chatHash(
          item.shortname,
          sprint.current_conversation_id,
        );
      };
      shellRow.append(pill);
    }
    rail.append(shellRow);
  }

  const side = el("aside", { className: "chat-history" });
  const newChat = el("button", {
    className: "act primary",
    type: "button",
    textContent: "＋ Chat",
  });
  const configure = el("button", {
    className: "chat-configure",
    type: "button",
    textContent: "Configure",
  });
  newChat.onclick = async () => {
    if (!await chatCloseForSwitch(selectedConversation)) return;
    newChat.disabled = true;
    configure.disabled = true;
    newChat.textContent = "Starting…";
    try {
      const conversation = await chatCreateConversation(shell);
      location.hash = chatHash(shell.shortname, conversation.conversation_id);
    } catch (error) {
      toast(`${error.code}: ${error.message}`);
      newChat.disabled = false;
      configure.disabled = false;
      newChat.textContent = "＋ Chat";
    }
  };
  configure.onclick = async () => {
    configure.disabled = true;
    if (!await chatCloseForSwitch(selectedConversation)) {
      configure.disabled = false;
      return;
    }
    location.hash = chatHash(shell.shortname, CHAT_CONFIGURE_ROUTE);
  };
  side.append(el("div", { className: "chat-history-head" },
    el("div", { className: "chat-history-shell" },
      el("div", {}, el("b", {}, shell.display_name),
        el("span", { className: "chat-shortname" }, ` /${shell.shortname}`)),
      configure),
    newChat));

  const history = el("div", { className: "chat-history-list" });
  const summaries = new Map();
  const historyItems = new Map();
  const recentIds = [];
  const starredIds = new Set();
  let moreCursor = recentPage.next_cursor;
  let moreInFlight = false;
  let starsInFlight = false;
  const starsStatus = el(
    "div",
    { className: "chat-history-status" },
    "Loading starred chats…",
  );
  const more = el("button", {
    className: "chat-history-more",
    type: "button",
    textContent: "More",
  });

  const acceptSummary = (conversation) => {
    const current = summaries.get(conversation.conversation_id);
    if (current && Number(current.version || 0) > Number(conversation.version || 0))
      return current;
    summaries.set(conversation.conversation_id, conversation);
    return conversation;
  };
  for (const conversation of recentPage.items) {
    acceptSummary(conversation);
    recentIds.push(conversation.conversation_id);
  }
  if (selectedConversation) acceptSummary(selectedConversation);

  const historyOrder = () => {
    const pinned = [...starredIds]
      .map((id) => summaries.get(id))
      .filter(Boolean)
      .sort((left, right) =>
        String(right.last_activity_at || "").localeCompare(
          String(left.last_activity_at || ""))
        || right.conversation_id.localeCompare(left.conversation_id));
    const recent = recentIds
      .map((id) => summaries.get(id))
      .filter((conversation) => conversation && !conversation.starred);
    const selected = selectedId && !pinned.some(
      (item) => item.conversation_id === selectedId)
      && !recent.some((item) => item.conversation_id === selectedId)
      ? summaries.get(selectedId)
      : null;
    return [...pinned, ...recent, ...(selected ? [selected] : [])];
  };

  const historyCard = (conversation) => {
    const card = el("div", {
      className: "chat-history-item"
        + (conversation.conversation_id === selectedId ? " selected" : ""),
    });
    const open = el("button", {
      className: "chat-history-open",
      type: "button",
    });
    const context = el("span", { className: "chat-history-context" });
    const name = el("span", { className: "chat-history-name" });
    const state = chatStatePill(conversation.state);
    const star = el("button", {
      className: "chat-history-star",
      type: "button",
    });
    const item = { card, open, context, name, state, star, conversation };
    open.append(context, name, state);
    card.append(open, star);
    chatPaintHistoryItem(item, conversation);
    open.onclick = async () => {
      const target = item.conversation;
      if (target.conversation_id === selectedId) return;
      if (await chatCloseForSwitch(selectedConversation))
        location.hash = chatHash(shell.shortname, target.conversation_id);
    };
    star.onclick = async (event) => {
      event.stopPropagation();
      star.disabled = true;
      try {
        const current = item.conversation;
        const updated = await chatApi(
          `/conversations/${current.conversation_id}`,
          "PATCH",
          { version: current.version, starred: !current.starred },
        );
        acceptSummary(updated);
        if (updated.starred) {
          starredIds.add(updated.conversation_id);
          const index = recentIds.indexOf(updated.conversation_id);
          if (index >= 0) recentIds.splice(index, 1);
        } else {
          starredIds.delete(updated.conversation_id);
          if (!recentIds.includes(updated.conversation_id)
              && updated.conversation_id === selectedId)
            recentIds.push(updated.conversation_id);
        }
        if (updated.conversation_id === selectedConversation?.conversation_id)
          selectedConversation = updated;
        renderHistory();
      } catch (error) {
        toast(`${error.code}: ${error.message}`);
      } finally {
        star.disabled = false;
      }
    };
    historyItems.set(conversation.conversation_id, item);
    return item;
  };

  const renderHistory = () => {
    const cards = historyOrder().map((conversation) => {
      let item = historyItems.get(conversation.conversation_id);
      if (!item) item = historyCard(conversation);
      chatPaintHistoryItem(item, conversation);
      item.card.classList.toggle(
        "selected",
        conversation.conversation_id === selectedId,
      );
      return item.card;
    });
    history.replaceChildren(...cards);
    if (!cards.length && !recentPage.error)
      history.append(
        el("div", { className: "chat-history-empty" }, "No chats yet."));
    if (recentPage.error)
      history.append(el(
        "div",
        { className: "chat-history-status error" },
        `Recent chats unavailable — ${recentPage.error.message}`,
      ));
    if (!starsStatus.hidden) history.append(starsStatus);
    if (moreCursor) history.append(more);
  };
  more.onclick = async () => {
    if (moreInFlight || !moreCursor) return;
    moreInFlight = true;
    more.disabled = true;
    more.textContent = "Loading…";
    const requestedCursor = moreCursor;
    try {
      const page = await chatApi(
        `/conversations?shell_id=${shell.shell_id}`
        + `&starred=false&limit=20&cursor=${encodeURIComponent(requestedCursor)}`,
      );
      if (generation !== chatRenderGeneration) return;
      for (const conversation of page.items) {
        acceptSummary(conversation);
        if (!recentIds.includes(conversation.conversation_id))
          recentIds.push(conversation.conversation_id);
      }
      moreCursor = page.next_cursor;
      more.textContent = "More";
      renderHistory();
    } catch (error) {
      moreCursor = requestedCursor;
      more.textContent = "Retry";
      toast(`${error.code}: ${error.message}`);
    } finally {
      moreInFlight = false;
      more.disabled = false;
    }
  };
  side.append(history);
  renderHistory();

  const pane = el("section", { className: "chat-pane" });
  layout.append(rail, side, pane);
  root.replaceChildren(layout);

  const loadStars = async () => {
    if (starsInFlight) return;
    starsInFlight = true;
    starsStatus.hidden = false;
    starsStatus.classList.remove("error");
    starsStatus.textContent = "Loading starred chats…";
    renderHistory();
    let cursor = null;
    try {
      do {
        const suffix = cursor
          ? `&cursor=${encodeURIComponent(cursor)}`
          : "";
        const page = await chatApi(
          `/conversations?shell_id=${shell.shell_id}`
          + `&starred=true&limit=100${suffix}`,
        );
        if (generation !== chatRenderGeneration) return;
        for (const conversation of page.items) {
          acceptSummary(conversation);
          starredIds.add(conversation.conversation_id);
        }
        cursor = page.next_cursor;
        renderHistory();
      } while (cursor);
      starsStatus.hidden = true;
    } catch (error) {
      const retry = el("button", {
        className: "chat-history-retry",
        type: "button",
        textContent: "Retry",
      });
      retry.onclick = loadStars;
      starsStatus.replaceChildren(
        `Starred chats unavailable — ${error.message} `,
        retry,
      );
      starsStatus.classList.add("error");
      starsStatus.hidden = false;
    } finally {
      starsInFlight = false;
    }
    renderHistory();
  };
  loadStars();

  let historyPollInFlight = false;
  const pollHistory = async () => {
    if (document.hidden || historyPollInFlight) return;
    historyPollInFlight = true;
    try {
      let cursor = null;
      let selectedSeen = false;
      const nextOpenByShell = new Map();
      do {
        const suffix = cursor
          ? `&cursor=${encodeURIComponent(cursor)}`
          : "";
        const page = await chatApi(
          `/conversations?open=true&limit=100${suffix}`,
        );
        for (const conversation of page.items) {
          nextOpenByShell.set(
            conversation.shell.shell_id,
            conversation.state || "idle",
          );
          const item = historyItems.get(conversation.conversation_id);
          if (item) {
            const accepted = acceptSummary(conversation);
            chatPaintHistoryItem(item, accepted);
          }
          if (conversation.conversation_id
              === selectedConversation?.conversation_id) {
            selectedSeen = true;
            selectedConversation = acceptSummary(conversation);
          }
        }
        cursor = page.next_cursor;
      } while (cursor);
      if (selectedConversation
          && selectedConversation.state !== "closed"
          && !selectedSeen) {
        const conversation = await chatApi(
          `/conversations/${selectedConversation.conversation_id}`,
        );
        const accepted = acceptSummary(conversation);
        const item = historyItems.get(conversation.conversation_id);
        if (item) chatPaintHistoryItem(item, accepted);
        selectedConversation = accepted;
      }
      if (generation !== chatRenderGeneration || !chatHistoryPollTimer) return;
      for (const [shellId, button] of shellItems)
        chatPaintShellState(button, nextOpenByShell.get(shellId));
    } catch { /* The next poll retries without disrupting the open chat. */ }
    finally { historyPollInFlight = false; }
  };
  chatHistoryPollTimer = setInterval(pollHistory, CHAT_HISTORY_POLL_MS);

  if (configuring) {
    const loadConfiguration = async () => {
      pane.replaceChildren(
        el("div", { className: "chat-loading" }, "Loading configuration…"));
      try {
        const { defaults, catalog } = await chatLoadConfiguration();
        if (generation !== chatRenderGeneration) return;
        await chatRenderNew(pane, shell, defaults, catalog);
      } catch (error) {
        if (generation !== chatRenderGeneration) return;
        const retry = el("button", {
          className: "act",
          type: "button",
          textContent: "Retry",
        });
        retry.onclick = loadConfiguration;
        pane.replaceChildren(el(
          "div",
          { className: "card chat-config-error" },
          el("div", {}, `Configuration unavailable — ${error.message}`),
          retry,
        ));
      }
    };
    await loadConfiguration();
    return;
  }
  if (!selectedId) {
    pane.append(el("div", { className: "chat-empty chat-no-selection" },
      "No chat selected."));
    return;
  }
  if (detailResult.error) {
    const retry = el("button", {
      className: "act",
      type: "button",
      textContent: "Retry",
    });
    retry.onclick = () => renderInterface(root);
    pane.replaceChildren(el(
      "div",
      { className: "card" },
      `Conversation unavailable — ${detailResult.error.message}`,
      retry,
    ));
    return;
  }
  const conversation = selectedConversation
    || await chatApi(`/conversations/${selectedId}`);
  const loadTranscript = async () => {
    pane.replaceChildren(
      el("div", { className: "chat-loading" }, "Loading transcript…"));
    try {
      const snapshot = await chatApi(
        `/conversations/${selectedId}/transcript`);
      if (generation !== chatRenderGeneration) return;
      await chatRenderOpen(pane, conversation, snapshot);
    } catch (error) {
      if (generation !== chatRenderGeneration) return;
      const retry = el("button", {
        className: "act",
        type: "button",
        textContent: "Retry",
      });
      retry.onclick = loadTranscript;
      const close = el("button", {
        className: "act danger",
        type: "button",
        textContent: "Close",
        disabled: conversation.scope === "sprint" || conversation.state === "closed",
        hidden: conversation.scope === "sprint",
      });
      close.onclick = async () => {
        close.disabled = true;
        try {
          const latest = await chatApi(
            `/conversations/${conversation.conversation_id}`);
          if (latest.state !== "closed") {
            await chatApi(
              `/conversations/${conversation.conversation_id}`,
              "PATCH",
              { version: latest.version, state: "closed" },
            );
          }
          await renderInterface(root);
        } catch (closeError) {
          close.disabled = false;
          toast(`${closeError.code}: ${closeError.message}`);
        }
      };
      pane.replaceChildren(el(
        "div",
        { className: "card chat-transcript-error" },
        el("h2", {}, chatHeaderLabel(conversation)),
        el("div", {}, `Transcript unavailable — ${error.message}`),
        el("div", { className: "row" }, retry, close),
      ));
    }
  };
  await loadTranscript();
}

// ── Sprints v2 FnB board ────────────────────────────────────────────────────
const SPRINTS_REFRESH_MS = 5000;
let sprintRouteId = null;      // null = priority selection; NaN = invalid exact route
let sprintSelectedId = null;
let sprintRenderGeneration = 0;
let sprintPollTimer = null;
let activeTab = "shells";
let sprintOpenUnitId = null;
let sprintLastGoodId = null;
let sprintFeedSprintId = null;
let sprintFeedState = {
  events: { open: false, openRows: new Set(), items: [], cursor: null, loading: false },
  summaries: { open: false, openRows: new Set(), items: [], cursor: null, loading: false },
};
const sprintFeedRefs = {};

const SPRINT_COLUMNS = [
  ["done", "Done"],
  ["review", "Review"],
  ["dev", "Dev"],
  ["waiting", "Waiting"],
  ["blocked", "Blocked"],
];

function sprintStopPolling() {
  if (sprintPollTimer !== null) clearTimeout(sprintPollTimer);
  sprintPollTimer = null;
}

function sprintPriority(items) {
  const newest = (rows, field) => rows.slice().sort((a, b) =>
    String(b[field] || "").localeCompare(String(a[field] || ""))
    || b.sprint_id - a.sprint_id)[0];
  return items.find((item) => item.lifecycle === "armed")
    || newest(items.filter((item) => item.lifecycle === "paused"), "paused_at")
    || newest(items.filter((item) => item.lifecycle === "prepared"), "created_at")
    || newest(items.filter((item) => ["completed", "aborted"].includes(item.lifecycle)), "created_at")
    || null;
}

function sprintScheduleRefresh(root, generation) {
  sprintStopPolling();
  if (activeTab !== "sprints" || document.hidden) return;
  sprintPollTimer = setTimeout(() => {
    if (generation !== sprintRenderGeneration || activeTab !== "sprints" || document.hidden) return;
    renderSprints(root, { refresh: true });
  }, SPRINTS_REFRESH_MS);
}

function sprintRoute(sprintId) {
  sprintRouteId = sprintId;
  sprintSelectedId = sprintId;
  location.hash = `sprints/${sprintId}`;
}

function sprintPageShell(list, selectedId) {
  const selector = el("select", {
    className: "sprint-selector",
    title: "Select Sprint",
  });
  for (const item of list) {
    const label = `Sprint ${item.sprint_id} · ${item.lifecycle} · ${item.feature.title}`;
    selector.append(el("option", {
      title: label,
      value: String(item.sprint_id),
      selected: item.sprint_id === selectedId,
      textContent: label.length > 50 ? `${label.slice(0, 49)}…` : label,
    }));
  }
  selector.onchange = () => sprintRoute(Number(selector.value));
  const content = el("div", { className: "sprint-content" });
  return {
    node: el("div", { className: "sprint-page" },
      el("div", { className: "sprint-toolbar" }, selector), content),
    content,
  };
}

function renderSprintRouteState(root, title, detail, retry = null) {
  const card = el("div", { className: "card sprint-route-state" },
    el("h2", {}, title), el("div", { className: "muted" }, detail));
  if (retry) {
    const button = el("button", { className: "act", type: "button", textContent: "Retry" });
    button.onclick = retry;
    card.append(button);
  }
  root.replaceChildren(card);
}

function sprintKeepLastGood(root, generation, error) {
  if (sprintLastGoodId === null || sprintLastGoodId !== sprintSelectedId || !root.firstChild)
    return false;
  root.querySelector?.(".sprint-stale-notice")?.remove();
  const retry = el("button", { className: "act", type: "button", textContent: "Retry now" });
  retry.onclick = () => renderSprints(root, { refresh: true });
  const notice = el("div", { className: "sprint-stale-notice", role: "status" },
    el("span", {}, `Showing the last good Sprint snapshot — refresh failed: ${error.message}`),
    retry);
  root.prepend(notice);
  sprintScheduleRefresh(root, generation);
  return true;
}

function sprintTimestamp(value) {
  if (!value) return null;
  const parsed = new Date(value.replace(" ", "T") + (value.includes("Z") ? "" : "Z"));
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function sprintElapsed(sprint) {
  const start = new Date((sprint.armed_at || sprint.created_at).replace(" ", "T") + "Z");
  const endValue = sprint.completed_at || sprint.aborted_at;
  const end = endValue ? new Date(endValue.replace(" ", "T") + "Z") : new Date();
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return [days ? `${days}d` : "", hours ? `${hours}h` : "", `${minutes}m`]
    .filter(Boolean).join(" ");
}

function sprintParticipantLink(person) {
  if (!person?.current_conversation_id) return el("span", {}, person?.shortname || "—");
  return el("a", {
    href: `#interface/${encodeURIComponent(person.shortname)}/${encodeURIComponent(person.current_conversation_id)}/chat`,
    textContent: person.shortname,
  });
}

function sprintAuditSection(title, rows, renderRow) {
  const section = el("div", { className: "sprint-audit-section" }, el("h3", {}, title));
  if (!rows.length) section.append(el("div", { className: "muted" }, "None"));
  else for (const row of rows) section.append(renderRow(row));
  return section;
}

function sprintFeedIdentity(kind, item) {
  return kind === "events" ? `event:${item.event_id}` : `${item.source}:${item.id}`;
}

function sprintFeedRow(kind, item, state) {
  const identity = sprintFeedIdentity(kind, item);
  const detail = el("details", {
    className: "sprint-feed-row",
    open: state.openRows.has(identity),
  });
  detail.ontoggle = () => {
    if (detail.open) state.openRows.add(identity);
    else state.openRows.delete(identity);
  };
  const label = kind === "events"
    ? `${item.actor.shortname || item.actor.kind} · ${item.type}`
    : `${item.author.shortname || "system"} · ${item.kind}`;
  detail.append(el("summary", {},
    el("span", {}, label),
    el("span", { className: "muted" }, sprintTimestamp(item.created_at))));
  if (kind === "events") {
    const keys = Object.keys(item.details || {});
    detail.append(keys.length
      ? el("pre", { className: "sprint-feed-detail" }, JSON.stringify(item.details, null, 2))
      : el("div", { className: "muted sprint-feed-detail" }, "No display details."));
  } else {
    detail.append(el("div", { className: "sprint-feed-detail" },
      item.work_unit_id ? el("div", { className: "muted" }, `Work unit U${item.work_unit_id}`) : "",
      el("div", { className: "sprint-long-text" }, item.body)));
  }
  return detail;
}

function sprintPaintFeed(kind) {
  const refs = sprintFeedRefs[kind];
  if (!refs) return;
  const state = sprintFeedState[kind];
  refs.list.replaceChildren();
  if (!state.items.length) refs.list.append(
    el("div", { className: "muted sprint-feed-empty" }, state.loading ? "Loading…" : "No entries."));
  else for (const item of state.items) refs.list.append(sprintFeedRow(kind, item, state));
  refs.more.hidden = !state.cursor;
  refs.more.disabled = state.loading;
  refs.more.textContent = state.loading ? "Loading…" : "Load more";
}

async function sprintLoadFeed(kind, { more = false, refresh = false } = {}) {
  const state = sprintFeedState[kind];
  if (state.loading || sprintFeedSprintId === null) return;
  const requestedSprintId = sprintFeedSprintId;
  state.loading = true;
  sprintPaintFeed(kind);
  try {
    const cursor = more ? state.cursor : null;
    const suffix = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
    const page = await api(`/sprints/${requestedSprintId}/${kind}?limit=50${suffix}`);
    if (
      requestedSprintId !== sprintFeedSprintId
      || requestedSprintId !== sprintSelectedId
      || state !== sprintFeedState[kind]
    ) return;
    if (!state.items.length || more) {
      state.items = more ? [...state.items, ...page.items] : page.items;
      state.cursor = page.next_cursor;
    } else if (refresh) {
      const seen = new Set(state.items.map((item) => sprintFeedIdentity(kind, item)));
      state.items = [
        ...page.items.filter((item) => !seen.has(sprintFeedIdentity(kind, item))),
        ...state.items,
      ];
    }
    const unique = new Set();
    state.items = state.items.filter((item) => {
      const identity = sprintFeedIdentity(kind, item);
      if (unique.has(identity)) return false;
      unique.add(identity);
      return true;
    });
  } catch (error) {
    toast(`${kind} unavailable: ${error.message}`);
  } finally {
    state.loading = false;
    if (state === sprintFeedState[kind]) sprintPaintFeed(kind);
  }
}

function sprintFeedsNode(sprintId) {
  if (sprintFeedSprintId !== sprintId) {
    sprintFeedSprintId = sprintId;
    sprintFeedState = {
      events: { open: false, openRows: new Set(), items: [], cursor: null, loading: false },
      summaries: { open: false, openRows: new Set(), items: [], cursor: null, loading: false },
    };
  }
  const wrap = el("div", { className: "sprint-feeds" });
  for (const [kind, label] of [["events", "Sprint events"], ["summaries", "Sprint summaries"]]) {
    const state = sprintFeedState[kind];
    const list = el("div", { className: "sprint-feed-list" });
    const more = el("button", { className: "act", type: "button", textContent: "Load more" });
    more.onclick = () => sprintLoadFeed(kind, { more: true });
    const detail = el("details", { className: "card sprint-feed", open: state.open },
      el("summary", {}, `${label} (${kind === "events" ? "timeline" : "judgments and reports"})`),
      list, more);
    detail.ontoggle = () => {
      state.open = detail.open;
      if (detail.open && !state.items.length) sprintLoadFeed(kind);
    };
    sprintFeedRefs[kind] = { detail, list, more };
    sprintPaintFeed(kind);
    wrap.append(detail);
  }
  return wrap;
}

function sprintScopedFeed(sprintId, workUnitId, kind, label) {
  const detail = el("details", { className: "sprint-audit-section sprint-scoped-feed" },
    el("summary", {}, label));
  const list = el("div", { className: "sprint-feed-list" });
  const more = el("button", { className: "act", type: "button", textContent: "Load more", hidden: true });
  let cursor = null;
  let items = [];
  let loading = false;
  const paint = () => {
    list.replaceChildren(...items.map((item) => sprintFeedRow(kind, item)));
    if (!items.length) list.append(el("div", { className: "muted" }, loading ? "Loading…" : "No entries."));
    more.hidden = !cursor;
    more.disabled = loading;
  };
  const loadPage = async () => {
    if (loading) return;
    loading = true; paint();
    try {
      const suffix = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
      const page = await api(
        `/sprints/${sprintId}/${kind}?limit=50&work_unit_id=${workUnitId}${suffix}`);
      items = [...items, ...page.items];
      cursor = page.next_cursor;
    } catch (error) { toast(`${label} unavailable: ${error.message}`); }
    finally { loading = false; paint(); }
  };
  detail.ontoggle = () => { if (detail.open && !items.length) loadPage(); };
  more.onclick = loadPage;
  detail.append(list, more);
  return detail;
}

function openSprintActionModal(sprint, target, label) {
  const reason = el("textarea", {
    rows: 5,
    maxlength: 2000,
    placeholder: `Reason for ${label.toLowerCase()} (required)`,
  });
  const confirmButton = el("button", { className: "act danger", type: "button", textContent: label });
  const cancel = el("button", { className: "act", type: "button", textContent: "Cancel" });
  const body = el("div", { className: "sprint-action-form" },
    target === "aborted" ? el("div", { className: "sprint-abort-note" },
      "Abort stops active work but retains the complete Sprint history and the Planner's durable abort-report request.") : "",
    el("label", { className: "k" }, "Reason"), reason);
  const close = openActionModal({
    title: `${label} · Sprint ${sprint.sprint_id}`,
    bodyNode: body,
    dismissNode: cancel,
    actionNode: confirmButton,
    width: 560,
    height: 360,
  });
  confirmButton.onclick = async () => {
    const value = reason.value.trim();
    if (!value) return toast("reason required");
    confirmButton.disabled = true;
    try {
      const result = await api(`/sprints/${sprint.sprint_id}`, "PATCH", {
        lifecycle: target,
        reason: value,
      });
      close();
      setStatus(result.changed ? `Sprint ${sprint.sprint_id} ${target}` : `Sprint already ${target}`);
      await renderSprints($("#view-sprints"), { refresh: true });
    } catch (error) {
      toast(`action failed: ${error.message}`);
      confirmButton.disabled = false;
    }
  };
  cancel.onclick = close;
  reason.focus();
}

function sprintActionButtons(sprint) {
  const actions = [];
  if (sprint.lifecycle === "armed") actions.push(["paused", "Pause Sprint"]);
  if (sprint.lifecycle === "paused") actions.push(["armed", "Resume Sprint"]);
  if (["prepared", "armed", "paused"].includes(sprint.lifecycle))
    actions.push(["aborted", "Abort Sprint"]);
  const row = el("div", { className: "sprint-actions" });
  for (const [target, label] of actions) {
    const button = el("button", {
      className: `act ${target === "aborted" ? "danger" : ""}`,
      type: "button",
      textContent: label,
    });
    button.onclick = () => openSprintActionModal(sprint, target, label);
    row.append(button);
  }
  return row;
}

function openSprintUnitModal(unit, snapshot) {
  sprintOpenUnitId = unit.work_unit_id;
  const body = el("div", { className: "sprint-unit-detail" });
  const facts = el("div", { className: "grid2 sprint-unit-facts" },
    el("span", { className: "k" }, "disposition"), el("span", {}, unit.disposition),
    el("span", { className: "k" }, "wave"), el("span", {}, String(unit.planned_wave)),
    el("span", { className: "k" }, "output kind"), el("span", {}, unit.output_kind),
    el("span", { className: "k" }, "developer"), sprintParticipantLink(unit.developer),
    el("span", { className: "k" }, "reviewer"), sprintParticipantLink(unit.reviewer),
    el("span", { className: "k" }, "prerequisites"),
    el("span", {}, unit.prerequisite_ids.length ? unit.prerequisite_ids.map((id) => `U${id}`).join(", ") : "None"),
    el("span", { className: "k" }, "dependents"),
    el("span", {}, unit.dependent_ids.length ? unit.dependent_ids.map((id) => `U${id}`).join(", ") : "None"));
  body.append(facts,
    el("h3", {}, "Expected output"), el("div", { className: "sprint-long-text" }, unit.expected_output));
  if (unit.completion_result) body.append(
    el("h3", {}, unit.disposition === "cancelled" ? "Cancellation result" : "Completion result"),
    el("div", { className: "sprint-long-text" }, unit.completion_result));

  body.append(sprintAuditSection("Included tasks", unit.tasks, (task) =>
    el("div", { className: "sprint-audit-row" },
      el("a", {
        href: `/api/documents/${task.document_id}/open`, target: "_blank", rel: "noopener",
        textContent: `#${task.task_id} ${task.title}`,
      }), el("span", { className: "pill" }, task.status))));
  body.append(sprintAuditSection("Pull requests", unit.pull_requests, (pr) => {
    const label = `${pr.repository}#${pr.pr_number}`;
    const link = pr.url ? el("a", {
      href: pr.url, target: "_blank", rel: "noopener", textContent: label,
    }) : el("span", {}, label);
    return el("div", { className: "sprint-audit-row" }, link,
      el("span", { className: "pill" }, pr.normalized_state || "registered"),
      el("span", { className: "muted" }, pr.observed_head_sha || "no head observed",
        pr.observed_at ? ` · ${sprintTimestamp(pr.observed_at)}` : ""));
  }));
  body.append(sprintAuditSection("Participant messages", unit.messages, (message) =>
    el("div", { className: "sprint-message-audit" },
      el("div", { className: "sprint-audit-row" },
        el("span", {}, `${message.sender?.shortname || "system"} → ${message.recipient.shortname}`),
        el("span", { className: "pill" }, message.kind),
        el("span", { className: "muted" }, sprintTimestamp(message.created_at))),
      el("div", { className: "sprint-long-text" }, message.body))));
  body.append(
    sprintScopedFeed(snapshot.sprint.sprint_id, unit.work_unit_id, "events", "Scoped events"),
    sprintScopedFeed(snapshot.sprint.sprint_id, unit.work_unit_id, "summaries", "Scoped judgments and reports"));

  const closeButton = el("button", { className: "act", type: "button", textContent: "Close" });
  const close = openModal({
    title: `U${unit.work_unit_id} · ${unit.title}`,
    bodyNode: body,
    footerStart: el("span", { className: "muted" }, `Sprint ${snapshot.sprint.sprint_id}`),
    footerEnd: closeButton,
    width: 840,
    height: 760,
  });
  closeButton.onclick = () => { sprintOpenUnitId = null; close(); };
}

function sprintWorkUnitCard(unit, snapshot) {
  const card = el("button", {
    className: `sprint-unit sprint-unit-${unit.disposition}`,
    type: "button",
    title: `U${unit.work_unit_id} ${unit.title}`,
  });
  card.dataset.unitId = String(unit.work_unit_id);
  const pr = unit.pull_requests[0];
  card.append(
    el("div", { className: "sprint-unit-title" },
      el("span", { className: "sprint-unit-id" }, `U${unit.work_unit_id}`),
      el("span", {}, unit.title)),
    el("div", { className: "sprint-unit-meta" },
      el("span", { className: "pill" }, unit.disposition),
      el("span", { className: "muted" }, `wave ${unit.planned_wave}`)),
    el("div", { className: "sprint-unit-people" },
      `Dev: ${unit.developer.shortname} · Review: ${unit.reviewer.shortname}`),
    el("div", { className: "sprint-unit-deps" },
      unit.prerequisite_ids.length
        ? `Depends: ${unit.prerequisite_ids.map((id) => `U${id}`).join(", ")}`
        : "Depends: none"),
    el("div", { className: "sprint-unit-foot" },
      el("span", {}, unit.output_kind.replaceAll("_", " ")),
      el("span", {}, pr ? `PR #${pr.pr_number} · ${pr.normalized_state || "registered"}` : "No PR")));
  if (unit.disposition === "cancelled") card.append(
    el("div", { className: "sprint-cancelled-note" }, "Cancelled — not completed"));
  card.onclick = () => openSprintUnitModal(unit, snapshot);
  return card;
}

function sprintWireGraph(wrap, canvas, svg, cardById, dependencies) {
  const draw = () => {
    if (!canvas.isConnected) return;
    const base = canvas.getBoundingClientRect();
    const width = canvas.scrollWidth;
    const height = canvas.scrollHeight;
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.replaceChildren();
    for (const edge of dependencies) {
      const source = cardById[edge.depends_on_work_unit_id];
      const target = cardById[edge.work_unit_id];
      if (!source || !target) continue;
      const a = source.getBoundingClientRect();
      const b = target.getBoundingClientRect();
      const x1 = a.right - base.left;
      const y1 = a.top - base.top + a.height / 2;
      const x2 = b.left - base.left;
      const y2 = b.top - base.top + b.height / 2;
      const bend = Math.max(36, Math.abs(x2 - x1) * .4);
      const path = document.createElementNS(SVGNS, "path");
      path.setAttribute("d", `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`);
      path.setAttribute("class", "sprint-wire");
      path.dataset.from = String(edge.depends_on_work_unit_id);
      path.dataset.to = String(edge.work_unit_id);
      svg.append(path);
    }
  };
  requestAnimationFrame(draw);
  const onResize = () => canvas.isConnected ? draw() : window.removeEventListener("resize", onResize);
  window.addEventListener("resize", onResize);

  const highlight = (unitId, on) => {
    const direct = new Set([String(unitId)]);
    for (const path of svg.querySelectorAll(".sprint-wire")) {
      const lit = path.dataset.from === String(unitId) || path.dataset.to === String(unitId);
      path.classList.toggle("lit", on && lit);
      if (lit) { direct.add(path.dataset.from); direct.add(path.dataset.to); }
    }
    wrap.classList.toggle("sprint-wire-focus", on);
    for (const [id, card] of Object.entries(cardById)) card.classList.toggle("lit", on && direct.has(id));
  };
  for (const [id, card] of Object.entries(cardById)) {
    card.onmouseenter = () => highlight(id, true);
    card.onmouseleave = () => highlight(id, false);
    card.onfocus = () => highlight(id, true);
    card.onblur = () => highlight(id, false);
  }
}

function sprintBoardNode(snapshot) {
  const sprint = snapshot.sprint;
  const header = el("div", { className: "card sprint-board-head" });
  const heading = el("div", { className: "sprint-heading" },
    el("h2", {}, `Sprint ${sprint.sprint_id}`),
    el("span", { className: `pill sprint-${sprint.lifecycle}` }, sprint.lifecycle));
  const feature = el("a", {
    href: `#roadmap-feature-${sprint.feature.feature_id}`,
    textContent: sprint.feature.title,
  });
  const specs = el("span", { className: "sprint-spec-links" });
  for (const spec of snapshot.specs) specs.append(" ", el("a", {
    href: `/api/documents/${spec.document_id}/open`, target: "_blank", rel: "noopener",
    textContent: `${spec.kind} #${spec.document_id}`,
  }));
  const times = [
    ["Created", sprint.created_at], ["Armed", sprint.armed_at],
    ["Paused", sprint.paused_at],
    [sprint.lifecycle === "aborted" ? "Aborted" : "Completed", sprint.aborted_at || sprint.completed_at],
  ].filter(([, value]) => value);
  header.append(heading,
    el("div", { className: "sprint-header-context" },
      el("span", {}, "Feature: ", feature),
      el("span", {}, `Planner: ${sprint.planner.shortname}`), specs),
    el("div", { className: "sprint-header-times" },
      ...times.map(([label, value]) => el("span", {}, `${label}: ${sprintTimestamp(value)}`)),
      el("span", {}, `Elapsed: ${sprintElapsed(sprint)}`)),
    sprintActionButtons(sprint));
  if (sprint.terminal_outcome) header.append(
    el("div", { className: "sprint-terminal-outcome" }, `Outcome: ${sprint.terminal_outcome}`));

  const scroll = el("div", { className: "sprint-board-scroll" });
  const canvas = el("div", { className: "sprint-board-canvas" });
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("class", "sprint-wires");
  svg.setAttribute("aria-hidden", "true");
  const columns = el("div", { className: "sprint-columns" });
  const cardById = {};
  for (const [key, label] of SPRINT_COLUMNS) {
    const units = snapshot.work_units.filter((unit) => unit.column === key);
    const column = el("section", { className: `sprint-column sprint-column-${key}` },
      el("h3", {}, label, el("span", { className: "count" }, String(units.length))));
    const cards = el("div", { className: "sprint-column-cards" });
    if (!units.length) cards.append(el("div", { className: "sprint-column-empty" }, "No work units"));
    for (const unit of units) {
      const card = sprintWorkUnitCard(unit, snapshot);
      cards.append(card);
      cardById[unit.work_unit_id] = card;
    }
    column.append(cards);
    columns.append(column);
  }
  canvas.append(svg, columns);
  scroll.append(canvas);
  sprintWireGraph(scroll, canvas, svg, cardById, snapshot.dependencies);
  return el("div", { className: "sprint-board-view" }, header, scroll,
    sprintFeedsNode(sprint.sprint_id));
}

async function renderSprints(root, { refresh = false } = {}) {
  const generation = ++sprintRenderGeneration;
  const previousScroll = refresh
    ? root.querySelector?.(".sprint-board-scroll")?.scrollLeft || 0
    : 0;
  const focusedUnitId = refresh ? document.activeElement?.dataset?.unitId || null : null;
  sprintStopPolling();
  if (!refresh || !root.firstChild) root.replaceChildren(el("div", { className: "muted" }, "Loading Sprints…"));
  if (Number.isNaN(sprintRouteId)) {
    renderSprintRouteState(root, "Sprint not found", "The Sprint route must contain a positive integer ID.");
    return;
  }
  let list;
  try {
    list = (await api("/sprints?limit=100")).items;
  } catch (error) {
    if (generation !== sprintRenderGeneration) return;
    if (refresh && sprintKeepLastGood(root, generation, error)) return;
    renderSprintRouteState(root, "Sprints unavailable", error.message, () => renderSprints(root));
    return;
  }
  if (generation !== sprintRenderGeneration) return;
  if (!list.length && sprintRouteId === null) {
    sprintSelectedId = null;
    renderSprintRouteState(
      root,
      "No Sprints yet",
      "Prepared Sprints appear here after declaration.",
    );
    return;
  }

  const preferred = sprintRouteId === null ? sprintPriority(list) : null;
  const selectedId = sprintRouteId ?? preferred?.sprint_id ?? null;
  if (selectedId === null) return;
  sprintSelectedId = selectedId;
  if (sprintRouteId === null && globalThis.history?.replaceState) {
    history.replaceState(null, "", `#sprints/${selectedId}`);
    sprintRouteId = selectedId;
  }

  let board;
  try {
    board = await api(`/sprints/${selectedId}`);
  } catch (error) {
    if (generation !== sprintRenderGeneration) return;
    if (refresh && sprintKeepLastGood(root, generation, error)) return;
    renderSprintRouteState(
      root,
      "Sprint unavailable",
      `Sprint ${selectedId} was not found or is not available: ${error.message}`,
      () => renderSprints(root),
    );
    return;
  }
  if (generation !== sprintRenderGeneration) return;
  if (!list.some((item) => item.sprint_id === selectedId)) {
    list = [{
      sprint_id: selectedId,
      lifecycle: board.sprint.lifecycle,
      feature: board.sprint.feature,
    }, ...list];
  }
  const shell = sprintPageShell(list, selectedId);
  shell.content.append(sprintBoardNode(board));
  root.replaceChildren(shell.node);
  sprintLastGoodId = selectedId;
  setDocumentTitle("sprints");
  requestAnimationFrame(() => {
    const scroll = root.querySelector?.(".sprint-board-scroll");
    if (scroll) scroll.scrollLeft = previousScroll;
    if (focusedUnitId)
      root.querySelector?.(`[data-unit-id="${focusedUnitId}"]`)?.focus();
  });
  for (const kind of ["events", "summaries"])
    if (sprintFeedState[kind].open) sprintLoadFeed(kind, { refresh: true });
  sprintScheduleRefresh(root, generation);
}

// ── Tabs + boot ────────────────────────────────────────────────────────────────
const VIEWS = {
  shells: ["#view-shells", renderShells],
  interface: ["#view-interface", renderInterface],
  sprints: ["#view-sprints", renderSprints],
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
// The tab title names the view and the fork (spec #43 U4), replacing a static
// per-fork string that named neither: every tab reads "<tab name> · <fork>".
// <fork> is the repo root basename, read from the same /health field the header
// breadcrumb renders, so the two can never disagree. It stays empty until
// /health answers — the title then omits the fork rather than naming one we
// have not confirmed.
let forkName = "";
function setDocumentTitle(tab) {
  const label = document.querySelector(
    `nav button[data-tab="${tab}"]`,
  )?.textContent || tab;
  const viewLabel = tab === "sprints" && sprintSelectedId
    ? `Sprint ${sprintSelectedId}` : label;
  document.title = forkName ? `${viewLabel} · ${forkName}` : viewLabel;
}
function show(tab) {
  activeTab = tab;
  for (const b of document.querySelectorAll("nav button")) b.classList.toggle("active", b.dataset.tab === tab);
  for (const k of Object.keys(VIEWS)) $(VIEWS[k][0]).hidden = k !== tab;
  document.body.classList.toggle("interface-view", tab === "interface");
  if (tab !== "interface") {
    chatStopStream();
    chatStopHistoryPoll();
    chatStopReview();
    chatModeController = null;
  }
  if (tab !== "sprints") sprintStopPolling();
  setDocumentTitle(tab);
  load(tab);
}
// Hash routing: the active tab lives in the URL (#roadmap), so a refresh stays
// put (and re-fetches that tab) instead of snapping back to Shells. Tabs set the
// hash; hashchange drives show — back/forward and deep links work too. The
// roadmap tab carries its sub-view in the hash: #roadmap (board) | #roadmap-flow.
// The analytics tab does the same: #analytics (token) | #analytics-quota.
// Shells: #shells (Harness) | #shells-skills | #shells-skill-assignments |
// #shells-default-models.
function routeFromHash() {
  const raw = location.hash.slice(1);
  if (raw === "interface" || raw.startsWith("interface/")) {
    const [, shell = "", conversation = "", requestedMode = ""] = raw.split("/");
    const nextMode = requestedMode === "diff" ? "diff" : "chat";
    chatRouteShell = decodeURIComponent(shell);
    chatRouteConversation = decodeURIComponent(conversation);
    const sameOpenConversation = Boolean(
      chatModeController
      && chatModeController.shell === chatRouteShell
      && chatModeController.conversationId === chatRouteConversation
    );
    chatRouteMode = nextMode;
    if (sameOpenConversation) {
      chatModeController.setMode(nextMode);
      return;
    }
    show("interface");
    return;
  }
  if (raw === "" || raw === "shells" || raw.startsWith("shells-")) {
    shellTab = Object.entries(SHELL_TAB_HASH).find(([, hash]) => hash === raw)?.[0]
      || "harness";
    show("shells");
    return;
  }
  if (raw === "sprints" || raw.startsWith("sprints/")) {
    const parts = raw.split("/");
    const requested = parts.length === 1 ? null : Number(parts[1]);
    sprintRouteId = parts.length === 1
      ? null
      : (parts.length === 2 && Number.isInteger(requested) && requested > 0 ? requested : NaN);
    show("sprints");
    return;
  }
  if (raw === "roadmap" || raw.startsWith("roadmap-")) {
    const featureMatch = raw.match(/^roadmap-feature-(\d+)$/);
    roadmapFeatureId = featureMatch ? Number(featureMatch[1]) : null;
    roadmapView = raw === "roadmap-flow" ? "flow" : "board";
    show("roadmap");
    return;
  }
  if (raw === "analytics" || raw.startsWith("analytics-")) {
    anView = raw === "analytics-quota" ? "quota" : "tokens";
    show("analytics");
    return;
  }
  if (!VIEWS[raw]) shellTab = "harness";
  show(VIEWS[raw] ? raw : "shells");
}
document.querySelectorAll("nav button").forEach((b) => (b.onclick = () => { location.hash = b.dataset.tab; }));
window.addEventListener("hashchange", routeFromHash);
window.addEventListener("popstate", routeFromHash);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) sprintStopPolling();
  else if (activeTab === "sprints") load("sprints");
});
// Close any open popover menu on an outside click (one handler for all .gmenu).
document.addEventListener("mousedown", (e) => {
  for (const m of document.querySelectorAll(".gmenu:not([hidden])"))
    if (!m.parentElement.contains(e.target)) m.hidden = true;
});
// Esc dismisses the topmost modal.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const overlays = document.querySelectorAll(".modal-overlay");
    const overlay = overlays[overlays.length - 1];
    if (overlay?.closeModal) overlay.closeModal();
  }
});
$("#snapshot").onclick = async () => {
  setStatus("saving locally…");
  try {
    const r = await api("/snapshot", "POST");
    toast(r.output || "done");
    setStatus("saved locally");
  }
  catch (e) { toast("error: " + e.message); }
};
(async () => {
  try {
    const h = await api("/health");
    $("#repo").textContent = h.repo;
    forkName = h.repo || "";
  }
  catch { setStatus("offline"); }
  routeFromHash();   // honor #tab on load (refresh / deep link), else Shells
})();
