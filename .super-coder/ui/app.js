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
  const close = openModal({ title: "New shell", bodyNode: form,
    footNodes: [cancel, create], width: 600, height: 300 });
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
  unauth: "token not usable — refreshes when you next use this harness",
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
    head.append(el("span", { className: "pill warn" },
      note + (prov.detail ? " · " + prov.detail : "")));
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

// ── Active sprints ───────────────────────────────────────────────────────────
// This is a read-only status surface. The API owns every displayed value; the
// browser owns only refresh timing, local duration arithmetic, and stale-state
// retention.
const SPRINTS_REFRESH_MS = 15000;
const SPRINTS_DURATION_MS = 60000;
const SPRINTS_SVG_NS = "http://www.w3.org/2000/svg";
const sprintsState = {
  payload: null,
  error: null,
  stale: false,
  active: false,
  root: null,
  refreshTimer: null,
  durationTimer: null,
  inFlight: null,
  lastFetchAt: 0,
  flowCleanups: [],
  renderedRoot: null,
  renderedSignature: null,
  selectedByDocument: new Map(),
};

function sprintsDuration(startedAt, now = Date.now()) {
  const started = Date.parse(startedAt);
  if (!Number.isFinite(started)) return null;
  // Negative deltas deliberately share the "<1m" zero bucket; a numeric
  // clamp would be dead code because the branch below already covers them.
  const minutes = Math.floor((now - started) / 60000);
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function sprintsUpdateNav(payload) {
  const button = document.querySelector('nav button[data-tab="sprints"]');
  if (!button) return;
  const count = payload.active_count;
  button.textContent = count > 0 ? `Sprints ${count}` : "Sprints";
  button.classList.toggle("warn", count > 0);
  button.title = "";
  button.hidden = false;
}

function sprintsHeader(sprint) {
  const header = el("div", { className: "sprint-header" },
    el("h2", {}, sprint.title),
    el("span", { className: "pill" }, `Doc #${sprint.document_id}`),
    el("span", { className: "pill" }, sprint.state || "unknown"));
  const planner = sprint.planner
    ? sprint.planner.shortname : "Unbound";
  const feature = sprint.feature
    ? `#${sprint.feature.feature_id} ${sprint.feature.title}` : "Unlinked";
  const meta = el("div", { className: "row sprint-meta" },
    el("span", {}, `Planner: ${planner}`),
    el("span", {}, `Feature: ${feature}`));
  if (sprint.qaqc) {
    meta.append(el(
      "span",
      { title: sprint.qaqc.body_sha256 },
      `QAQC: ${sprint.qaqc.verdict} #${sprint.qaqc.review_id}`,
    ));
  }
  const routes = [
    sprint.planner_route && `Planner ${sprint.planner_route}`,
    sprint.dev_route && `Dev ${sprint.dev_route}`,
    sprint.reviewer_route && `Reviewer ${sprint.reviewer_route}`,
  ].filter(Boolean);
  if (routes.length) meta.append(el("span", {}, `Routes: ${routes.join(" · ")}`));
  if (sprint.started_at) {
    const started = new Date(sprint.started_at);
    const duration = sprintsDuration(sprint.started_at);
    const durationNode = el("span", { className: "sprint-duration" },
      `Running: ${duration}`);
    durationNode.dataset.startedAt = sprint.started_at;
    meta.append(
      el("time", { dateTime: sprint.started_at, title: sprint.started_at },
        `Started: ${started.toLocaleString()}`),
      durationNode);
  }
  return [header, meta];
}

// Most-advanced state first: dependency wires run prerequisite → dependent,
// and a prerequisite is almost always further along the pipeline than what
// waits on it, so this order makes the dominant wire direction left→right.
const SPRINT_FLOW_COLUMNS = [
  { key: "done", label: "Done" },
  { key: "in_review", label: "Review" },
  { key: "working", label: "Dev" },
  { key: "pending", label: "Waiting" },
  { key: "blocked", label: "Blocked" },
];
const SPRINT_STATE_LABELS = {
  pending: "Pending",
  working: "Working",
  in_review: "In Review",
  blocked: "Blocked",
  merged: "Merged",
  cancelled: "Cancelled",
};

function sprintsColumnKey(unit) {
  if (unit.state_recognized === false) return "unrecognized";
  if (unit.state === "merged" || unit.state === "cancelled") return "done";
  return SPRINT_FLOW_COLUMNS.some((column) => column.key === unit.state)
    ? unit.state : "unrecognized";
}

function sprintsRole(unit, role, emphasized) {
  const shellId = unit[`${role}_shell_id`];
  const shortname = unit[`${role}_shortname`];
  const label = shortname || (shellId == null ? "Unassigned" : `Shell #${shellId}`);
  const classes = ["sprint-role"];
  if (emphasized) classes.push("active");
  if (!shortname) classes.push("warn");
  return el("span", { className: classes.join(" ") },
    `${role === "dev" ? "Dev" : "Reviewer"}: ${label}`);
}

function sprintsUnitCard(unit, columnKey, unavailable) {
  const card = el("article", {
    className: `sprint-unit ${columnKey}`,
    title: `${unit.seq} ${unit.unit_title}`,
    role: "button",
    tabIndex: 0,
    ariaPressed: "false",
  });
  card.dataset.seq = String(unit.seq);
  card.append(
    el("div", { className: "sprint-unit-title", title: unit.unit_title },
      el("span", { className: "idnum" }, unit.seq), " ", unit.unit_title),
    el("div", { className: "sprint-unit-state" },
      el("span", { className: `pill ${columnKey}` },
        Object.hasOwn(SPRINT_STATE_LABELS, unit.state)
          ? SPRINT_STATE_LABELS[unit.state] : String(unit.state))));

  const roles = el("div", { className: "sprint-unit-roles" },
    sprintsRole(unit, "dev", columnKey === "working"),
    sprintsRole(unit, "reviewer", columnKey === "in_review"));
  card.append(roles);

  if (unit.depends_on) {
    const deps = el("div", { className: "sprint-unit-deps" },
      `Depends: ${unit.depends_on}`);
    if (unavailable.length) {
      const warning = `dependency unavailable: ${unavailable.join(", ")}`;
      deps.append(" ", el("span", {
        className: "pill warn sprint-dep-warning",
        role: "img",
        ariaLabel: warning,
        title: warning,
      }, "⚠"));
    }
    card.append(deps);
  }
  const details = el("div", {
    className: "sprint-unit-details",
    hidden: true,
  });
  if (unit.overlap) {
    details.append(el("div", {
      className: "sprint-unit-overlap",
      title: unit.overlap,
    }, unit.overlap));
  }
  if (unit.branch || unit.pr_number != null) {
    const delivery = el("div", { className: "sprint-unit-delivery" });
    if (unit.branch)
      delivery.append(el("span", { title: unit.branch }, `Branch: ${unit.branch}`));
    if (unit.pr_number != null)
      delivery.append(el("span", {}, `PR #${unit.pr_number}`));
    details.append(delivery);
  }
  if (details.children.length) card.append(details);
  return card;
}

// One graph per sprint. Resolving dependencies against this function's unit map
// makes a cross-sprint edge impossible by construction.
function sprintsBuildFlow(sprint) {
  const units = sprint.units || [];
  const wrap = el("div", { className: "sprint-flow" });
  if (!units.length) {
    wrap.append(el("div", { className: "muted" }, "No units declared"));
    return wrap;
  }

  const bySeq = new Map(units.map((unit) => [String(unit.seq), unit]));
  const edges = [];
  const unavailableBySeq = new Map();
  const seenEdges = new Set();
  for (const unit of units) {
    const raw = String(unit.depends_on || "");
    if (!raw) continue;
    const unavailable = [];
    for (const part of raw.split(",")) {
      const token = part.trim();
      if (!token || token === String(unit.seq) || !bySeq.has(token)) {
        unavailable.push(token || "(empty)");
        continue;
      }
      // A discharged prerequisite (Done column) no longer constrains anything;
      // its wire would be pure ink. The card's Depends: line still names it.
      if (sprintsColumnKey(bySeq.get(token)) === "done") continue;
      const edgeKey = `${token}\u0000${unit.seq}`;
      if (!seenEdges.has(edgeKey)) {
        seenEdges.add(edgeKey);
        edges.push([token, String(unit.seq)]);
      }
    }
    if (unavailable.length) unavailableBySeq.set(String(unit.seq), unavailable);
  }

  const inner = el("div", { className: "sprint-flow-inner" });
  const svg = document.createElementNS(SPRINTS_SVG_NS, "svg");
  svg.setAttribute("class", "sprint-wires");
  svg.setAttribute("aria-hidden", "true");
  const cols = el("div", { className: "sprint-cols" });
  const cardOf = new Map();
  const columns = [...SPRINT_FLOW_COLUMNS];
  if (units.some((unit) => sprintsColumnKey(unit) === "unrecognized"))
    columns.push({ key: "unrecognized", label: "Unrecognized" });

  // Row order inside a column follows the wires, not declaration order: two
  // barycenter sweeps pull each card level with its graph neighbours, which
  // removes most wire crossings without changing what any column contains.
  // Cards with no live dependencies keep their relative declaration order.
  const columnUnits = new Map(columns.map((column) => [column.key,
    units.filter((unit) => sprintsColumnKey(unit) === column.key)]));
  const neighbors = new Map();
  for (const [from, to] of edges) {
    if (!neighbors.has(from)) neighbors.set(from, []);
    if (!neighbors.has(to)) neighbors.set(to, []);
    neighbors.get(from).push(to);
    neighbors.get(to).push(from);
  }
  const rowOf = new Map();
  for (const inColumn of columnUnits.values())
    inColumn.forEach((unit, row) => rowOf.set(String(unit.seq), row));
  for (let sweep = 0; sweep < 2; sweep += 1) {
    for (const inColumn of columnUnits.values()) {
      const keys = new Map(inColumn.map((unit) => {
        const seq = String(unit.seq);
        const near = neighbors.get(seq);
        return [seq, near && near.length
          ? near.reduce((sum, other) => sum + rowOf.get(other), 0) / near.length
          : rowOf.get(seq)];
      }));
      inColumn.sort((a, b) =>
        keys.get(String(a.seq)) - keys.get(String(b.seq)));
      inColumn.forEach((unit, row) => rowOf.set(String(unit.seq), row));
    }
  }

  for (const column of columns) {
    const inColumn = columnUnits.get(column.key);
    const heading = el("div", { className: "sprint-col-head" }, column.label);
    if (inColumn.length)
      heading.append(el("span", { className: "count" }, String(inColumn.length)));
    const col = el("div", { className: `sprint-col ${column.key}` }, heading);
    for (const unit of inColumn) {
      const seq = String(unit.seq);
      const card = sprintsUnitCard(
        unit, column.key, unavailableBySeq.get(seq) || []);
      col.append(card);
      cardOf.set(seq, card);
    }
    cols.append(col);
  }
  inner.append(svg, cols);
  wrap.append(inner);

  const markerId = `sprint-arrow-${sprint.document_id}`;
  const draw = () => {
    if (!inner.isConnected) return;
    const base = inner.getBoundingClientRect();
    const width = inner.scrollWidth;
    const height = inner.scrollHeight;
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    const marker = document.createElementNS(SPRINTS_SVG_NS, "marker");
    marker.setAttribute("id", markerId);
    marker.setAttribute("viewBox", "0 0 8 8");
    marker.setAttribute("refX", "7");
    marker.setAttribute("refY", "4");
    marker.setAttribute("markerWidth", "6");
    marker.setAttribute("markerHeight", "6");
    marker.setAttribute("orient", "auto-start-reverse");
    const arrow = document.createElementNS(SPRINTS_SVG_NS, "path");
    arrow.setAttribute("d", "M0 0 L8 4 L0 8 z");
    arrow.setAttribute("fill", "context-stroke");
    marker.append(arrow);
    const defs = document.createElementNS(SPRINTS_SVG_NS, "defs");
    defs.append(marker);
    svg.replaceChildren(defs);

    for (const [from, to] of edges) {
      const source = cardOf.get(from);
      const target = cardOf.get(to);
      if (!source || !target) continue;
      const a = source.getBoundingClientRect();
      const z = target.getBoundingClientRect();
      const y1 = a.top - base.top + a.height / 2;
      const y2 = z.top - base.top + z.height / 2;
      // Anchor on the near edges: a backward wire exits left and enters right
      // instead of looping the full board width, and a same-column wire takes
      // a short lap through the gutter beside its own column.
      let d;
      if (z.left >= a.right) {
        const x1 = a.right - base.left;
        const x2 = z.left - base.left;
        const dx = Math.max(40, (x2 - x1) * 0.4);
        d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
      } else if (z.right <= a.left) {
        const x1 = a.left - base.left;
        const x2 = z.right - base.left;
        const dx = Math.max(40, (x1 - x2) * 0.4);
        d = `M ${x1} ${y1} C ${x1 - dx} ${y1}, ${x2 + dx} ${y2}, ${x2} ${y2}`;
      } else {
        const x1 = a.right - base.left;
        const x2 = z.right - base.left;
        d = `M ${x1} ${y1} C ${x1 + 24} ${y1}, ${x2 + 24} ${y2}, ${x2} ${y2}`;
      }
      const path = document.createElementNS(SPRINTS_SVG_NS, "path");
      path.setAttribute("d", d);
      path.setAttribute("class", "sprint-wire");
      path.setAttribute("marker-end", `url(#${markerId})`);
      path.dataset.from = from;
      path.dataset.to = to;
      svg.append(path);
    }
  };
  requestAnimationFrame(draw);

  const onResize = () => {
    if (inner.isConnected) draw();
    else window.removeEventListener("resize", onResize);
  };
  window.addEventListener("resize", onResize);
  sprintsState.flowCleanups.push(
    () => window.removeEventListener("resize", onResize));

  let selectedSeq = sprintsState.selectedByDocument.get(sprint.document_id) || null;
  if (selectedSeq && !cardOf.has(selectedSeq)) {
    sprintsState.selectedByDocument.delete(sprint.document_id);
    selectedSeq = null;
  }

  const spotlight = (seq) => {
    const lit = new Set(seq ? [seq] : []);
    wrap.classList.toggle("sprint-spotlight", Boolean(seq));
    for (const wire of svg.querySelectorAll(".sprint-wire")) {
      const incident = seq
        && (wire.dataset.from === seq || wire.dataset.to === seq);
      wire.classList.toggle("lit", Boolean(incident));
      if (incident) {
        lit.add(wire.dataset.from);
        lit.add(wire.dataset.to);
      }
    }
    for (const [otherSeq, other] of cardOf)
      other.classList.toggle("lit", lit.has(otherSeq));
  };

  const select = (seq) => {
    selectedSeq = seq;
    if (seq) sprintsState.selectedByDocument.set(sprint.document_id, seq);
    else sprintsState.selectedByDocument.delete(sprint.document_id);
    for (const [otherSeq, other] of cardOf) {
      const selected = otherSeq === seq;
      const details = other.querySelectorAll(".sprint-unit-details")[0];
      other.classList.toggle("selected", selected);
      other.ariaPressed = String(selected);
      if (details) {
        details.hidden = !selected;
        other.ariaExpanded = String(selected);
      }
    }
    spotlight(seq);
  };

  for (const [seq, card] of cardOf) {
    card.onmouseenter = () => {
      if (!selectedSeq) spotlight(seq);
    };
    card.onmouseleave = () => {
      if (!selectedSeq) spotlight(null);
    };
    card.onclick = (event) => {
      event.stopPropagation();
      select(selectedSeq === seq ? null : seq);
    };
    card.onkeydown = (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      select(selectedSeq === seq ? null : seq);
    };
  }
  wrap.onclick = () => select(null);
  select(selectedSeq);
  return wrap;
}

function sprintsRenderedProjection(payload) {
  return (payload?.sprints || []).map((sprint) => ({
    document_id: sprint.document_id,
    title: sprint.title,
    state: sprint.state,
    started_at: sprint.started_at,
    planner_route: sprint.planner_route,
    dev_route: sprint.dev_route,
    reviewer_route: sprint.reviewer_route,
    qaqc: sprint.qaqc ? {
      review_id: sprint.qaqc.review_id,
      verdict: sprint.qaqc.verdict,
      body_sha256: sprint.qaqc.body_sha256,
    } : null,
    planner: sprint.planner
      ? { shortname: sprint.planner.shortname } : null,
    feature: sprint.feature ? {
      feature_id: sprint.feature.feature_id,
      title: sprint.feature.title,
    } : null,
    units: (sprint.units || []).map((unit) => ({
      seq: unit.seq,
      unit_title: unit.unit_title,
      state: unit.state,
      state_recognized: unit.state_recognized,
      dev_shell_id: unit.dev_shell_id,
      dev_shortname: unit.dev_shortname,
      reviewer_shell_id: unit.reviewer_shell_id,
      reviewer_shortname: unit.reviewer_shortname,
      depends_on: unit.depends_on,
      overlap: unit.overlap,
      branch: unit.branch,
      pr_number: unit.pr_number,
    })),
  }));
}

function sprintsPaint() {
  const root = sprintsState.root;
  if (!sprintsState.active || !root || !root.isConnected) return;
  const signature = JSON.stringify({
    loaded: sprintsState.lastFetchAt !== 0,
    projection: sprintsRenderedProjection(sprintsState.payload),
    error: sprintsState.error,
    stale: sprintsState.stale,
  });
  if (root === sprintsState.renderedRoot
      && signature === sprintsState.renderedSignature) return;
  for (const cleanup of sprintsState.flowCleanups) cleanup();
  sprintsState.flowCleanups = [];
  if (!sprintsState.payload) {
    const message = sprintsState.lastFetchAt
      ? "error: " + (sprintsState.error || "request failed")
      : "Loading active sprints…";
    root.replaceChildren(el("div", { className: "card" }, message));
    sprintsState.renderedRoot = root;
    sprintsState.renderedSignature = signature;
    return;
  }

  const nodes = [];
  if (sprintsState.stale) {
    nodes.push(el("div", { className: "card" },
      "Stale — refresh failed: " + sprintsState.error));
  }
  const sprints = sprintsState.payload.sprints || [];
  if (!sprints.length)
    nodes.push(el("div", { className: "card" }, "No active sprints."));
  for (const sprint of sprints) {
    nodes.push(el("section", { className: "card sprint-board" },
      ...sprintsHeader(sprint), sprintsBuildFlow(sprint)));
  }
  root.replaceChildren(...nodes);
  sprintsState.renderedRoot = root;
  sprintsState.renderedSignature = signature;
}

function sprintsUpdateDurations() {
  const root = sprintsState.root;
  if (!sprintsState.active || !root || !root.isConnected) return;
  for (const duration of root.querySelectorAll(".sprint-duration")) {
    duration.textContent =
      `Running: ${sprintsDuration(duration.dataset.startedAt)}`;
  }
}

async function sprintsRefresh({ render = true } = {}) {
  if (sprintsState.inFlight) return sprintsState.inFlight;
  const request = (async () => {
    try {
      const payload = await api("/sprints?status=active");
      if (!Number.isFinite(payload?.active_count))
        throw new Error("invalid active sprint projection");
      sprintsState.payload = payload;
      sprintsState.error = null;
      sprintsState.stale = false;
      sprintsUpdateNav(payload);
    } catch (error) {
      sprintsState.error = error.message;
      sprintsState.stale = sprintsState.payload !== null;
      if (!sprintsState.stale) {
        const button = document.querySelector(
          'nav button[data-tab="sprints"]');
        if (button) {
          button.title = "Active sprint count unavailable";
          button.hidden = false;
        }
      }
    } finally {
      sprintsState.lastFetchAt = Date.now();
      if (render) sprintsPaint();
    }
  })();
  sprintsState.inFlight = request;
  try { await request; }
  finally {
    if (sprintsState.inFlight === request) sprintsState.inFlight = null;
  }
}

function sprintsPoll() {
  if (document.hidden) return;
  return sprintsRefresh();
}

function sprintsStopPoll() {
  if (sprintsState.refreshTimer !== null)
    clearInterval(sprintsState.refreshTimer);
  document.removeEventListener("visibilitychange", sprintsPoll);
  sprintsState.refreshTimer = null;
}

function sprintsStartPoll() {
  sprintsStopPoll();
  document.addEventListener("visibilitychange", sprintsPoll);
  sprintsState.refreshTimer = setInterval(sprintsPoll, SPRINTS_REFRESH_MS);
}

function sprintsStopRender() {
  if (sprintsState.durationTimer !== null)
    clearInterval(sprintsState.durationTimer);
  for (const cleanup of sprintsState.flowCleanups) cleanup();
  sprintsState.flowCleanups = [];
  sprintsState.active = false;
  sprintsState.root = null;
  sprintsState.durationTimer = null;
  sprintsState.renderedRoot = null;
  sprintsState.renderedSignature = null;
}

async function renderSprints(root) {
  sprintsStopRender();
  sprintsState.active = true;
  sprintsState.root = root;
  sprintsPaint();
  sprintsState.durationTimer =
    setInterval(sprintsUpdateDurations, SPRINTS_DURATION_MS);
}

// ── Interface / browser-native conversations ────────────────────────────────
// A browser conversation owns the shell's single session slot until ended. It
// uses the normal CLI preparation path server-side, but deliberately has no
// terminal, tmux controls, or live hand-off between browser and CLI.
const CHAT_HARNESSES = ["opencode", "claude", "codex"];
const CHAT_FLAVOR_ORDER = ["cartographer", "admin", "planner", "dev", "reviewer", "devops"];
const CHAT_CONFIGURE_ROUTE = "configure";
let chatRouteShell = "";
let chatRouteConversation = "";
let chatSource = null;
let chatRenderGeneration = 0;
let chatPendingSend = null;

function chatStopStream() {
  if (chatSource) chatSource.close();
  chatSource = null;
}

function chatHash(shortname, conversationId = "") {
  return `interface/${encodeURIComponent(shortname)}`
    + (conversationId ? `/${encodeURIComponent(conversationId)}` : "");
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
  pill.append(
    label,
    el("span", { className: "chat-working-dots", ariaHidden: "true" },
      el("span", {}, "."),
      el("span", {}, "."),
      el("span", {}, ".")),
  );
  return pill;
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
  if (!conversation || conversation.state === "closed") return true;
  if (!["idle", "waiting", "error"].includes(conversation.state)) {
    toast("Finish the current turn and queued messages before switching chats.");
    return false;
  }
  try {
    await chatApi(
      `/conversations/${conversation.conversation_id}`,
      "PATCH",
      { version: conversation.version, state: "closed" },
    );
    conversation.state = "closed";
    return true;
  } catch (error) {
    toast(`${error.code}: ${error.message}`);
    return false;
  }
}

function chatActivity(events) {
  return events.filter((event) => [
    "permission.requested",
    "input.requested",
    "run.failed",
    "run.interrupted",
    "run.unknown",
  ].includes(event.event_type));
}

function chatAssistantRuns(events) {
  const runs = [];
  const byId = new Map();
  for (const event of events) {
    if (event.event_type !== "assistant.delta") continue;
    const id = event.run_id || `message-${event.message_id || "unknown"}`;
    let run = byId.get(id);
    if (!run) {
      run = {
        id,
        text: "",
        created_at: event.created_at,
        message_id: event.message_id,
        sequence: event.sequence,
      };
      byId.set(id, run);
      runs.push(run);
    }
    run.text += event.payload?.text || "";
  }
  return runs;
}

function chatBubble(kind, body, meta = "", conversation = null) {
  const bubble = el("article", { className: `chat-bubble chat-${kind}` });
  bubble.append(el("div", { className: "chat-who" },
    kind === "user" ? "You"
      : kind === "assistant" ? chatShellLabel(conversation)
      : "Activity"));
  bubble.append(kind === "activity"
    ? el("div", { className: "chat-activity-text" }, body)
    : mdBlock(body));
  if (meta) bubble.append(el("div", { className: "chat-meta" }, meta));
  return bubble;
}

function chatTranscriptAtBottom(transcript) {
  return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight <= 32;
}

function chatPaintTranscript(
  transcript, messages, events, conversation, retry, shouldFollow, onPosition,
) {
  const previousTop = transcript.scrollTop;
  const assistants = chatAssistantRuns(events);
  const activities = chatActivity(events);
  const items = [];
  const addActivity = (activity) => {
    const payload = activity.payload || {};
    const type = activity.event_type;
    const label = type === "permission.requested" ? "Waiting for permission"
      : type === "input.requested" ? "Waiting for input"
      : type === "run.interrupted" ? "Turn interrupted"
      : type === "run.unknown" ? "Turn outcome could not be proven"
      : `Turn failed${payload.error ? ` — ${payload.error}` : ""}`;
    items.push({
      node: chatBubble("activity", label),
      failed: false,
    });
  };
  for (const message of [...messages].sort((a, b) => a.message_id - b.message_id)) {
    if (message.message_kind === "control") continue;
    items.push({
      node: chatBubble("user", message.body, message.state),
      failed: message.state === "failed",
      text: message.body,
    });
    for (const run of assistants.filter((item) => item.message_id === message.message_id))
      items.push({
        node: chatBubble("assistant", run.text, "", conversation),
        failed: false,
      });
    for (const activity of activities.filter(
      (item) => item.message_id === message.message_id))
      addActivity(activity);
  }
  // Conversation-scoped events are uncommon, but keeping them visible is
  // better than dropping a recovery/error signal whose message link is absent.
  for (const run of assistants.filter((item) => item.message_id == null))
    items.push({
      node: chatBubble("assistant", run.text, "", conversation),
      failed: false,
    });
  for (const activity of activities) {
    if (activity.message_id == null) addActivity(activity);
  }
  for (const item of items) {
    if (item.failed) {
      const button = el("button", {
        className: "chat-retry",
        type: "button",
        textContent: "Retry",
      });
      button.onclick = () => retry(item.text);
      item.node.append(button);
    }
  }
  if (!items.length) {
    items.push({
      node: el("div", { className: "chat-empty" },
        `Start a conversation with ${conversation.shell.display_name}.`),
      failed: false,
    });
  }
  transcript.replaceChildren(...items.map((item) => item.node));
  requestAnimationFrame(() => {
    transcript.scrollTop = shouldFollow() ? transcript.scrollHeight : previousTop;
    onPosition();
  });
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

function chatOpenStream(conversationId, generation, onEvent, onState) {
  chatStopStream();
  const source = new EventSource(`/api/conversations/${conversationId}/events`);
  chatSource = source;
  source.onopen = () => onState("connected");
  source.onerror = () => onState("reconnecting");
  const types = [
    "conversation.created", "conversation.updated", "conversation.renamed",
    "conversation.closed", "message.accepted", "session.started", "run.started",
    "assistant.delta", "tool.started", "tool.completed", "permission.requested",
    "input.requested", "usage", "run.completed", "run.failed",
    "run.interrupted", "run.unknown",
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

async function chatRenderNew(host, shell, defaults, catalog) {
  const rows = defaults.flavors?.[shell.flavor] || [];
  const byHarness = Object.fromEntries(rows.map((row) => [row.harness, row]));
  const defaultHarness = rows.find((row) => row.is_default)?.harness;
  const availableHarnesses = shell.flavor === "conductor"
    ? ["opencode"] : CHAT_HARNESSES;
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

async function chatRenderOpen(host, initialConversation, initialMessages) {
  const generation = chatRenderGeneration;
  let conversation = initialConversation;
  let messages = [...initialMessages];
  const events = [];
  const seen = new Set();
  let latestRunId = null;

  const header = el("div", { className: "chat-pane-head" });
  const title = el("div", { className: "chat-pane-title" });
  const state = el("div", { className: "chat-pane-state" });
  const queueState = el("span", { className: "chat-queue-state", hidden: true });
  const streamState = el("span", { className: "chat-stream-state" }, "connecting");
  const actions = el("div", { className: "chat-actions" });
  const transcriptHost = el("div", { className: "chat-transcript-host" });
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
    title: "Reserved for future stream control",
    disabled: true,
  });
  const pending = el("div", { className: "chat-pending", hidden: true });
  const composerRow = el("div", { className: "chat-composer" },
    composer, el("div", { className: "chat-compose-actions" }, pending, send, stop));

  const retry = async (text) => {
    composer.value = text;
    composer.focus();
    await submit();
  };
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
    state.replaceChildren(
      chatStatePill(conversation.state), queueState, streamState);
    const closed = conversation.state === "closed";
    composer.disabled = closed;
    send.disabled = closed;
    interrupt.disabled = !["queued", "running", "waiting"].includes(conversation.state);
    close.disabled = !["idle", "waiting", "error"].includes(conversation.state);
    composer.placeholder = closed ? "This conversation is closed." : "Message this shell…";
    chatPaintTranscript(
      transcript,
      messages,
      events,
      conversation,
      retry,
      () => followTranscriptTail,
      updateTranscriptFollow,
    );
  };
  const refresh = () => chatRefreshConversation(
    conversation.conversation_id,
    generation,
    (next, nextMessages) => {
      conversation = next;
      messages = nextMessages;
      paint();
    });

  const analytics = el("button", { className: "act", type: "button", textContent: "Analytics" });
  analytics.onclick = () => {
    anFilters.harness = conversation.route.harness || "";
    anFilters.model = conversation.route.model || "";
    location.hash = "analytics";
  };
  const interrupt = el("button", { className: "act", type: "button", textContent: "Interrupt" });
  interrupt.onclick = async () => {
    interrupt.disabled = true;
    try {
      await chatApi(`/conversations/${conversation.conversation_id}/interruptions`,
        "POST", latestRunId ? { run_id: latestRunId } : {}, requestKey());
    } catch (error) { toast(`${error.code}: ${error.message}`); }
    finally { interrupt.disabled = false; }
  };
  const close = el("button", {
    className: "act danger",
    type: "button",
    textContent: "Close",
  });
  close.onclick = async () => {
    if (!confirm(
      "Close this conversation? Its transcript will remain available."
    )) return;
    try {
      conversation = await chatApi(`/conversations/${conversation.conversation_id}`,
        "PATCH", { version: conversation.version, state: "closed" });
      paint();
    } catch (error) { toast(`${error.code}: ${error.message}`); refresh(); }
  };
  actions.append(analytics, interrupt, close);
  header.append(title, state, actions);

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
      send.disabled = conversation.state === "closed";
    }
  }
  send.onclick = submit;
  composer.onkeydown = (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };
  host.replaceChildren(header, transcriptHost, composerRow);
  paint();
  chatOpenStream(
    conversation.conversation_id,
    generation,
    (event) => {
      if (seen.has(event.sequence)) return;
      seen.add(event.sequence);
      events.push(event);
      if (event.run_id) latestRunId = event.run_id;
      const message = messages.find((item) => item.message_id === event.message_id);
      if (message && event.event_type === "run.started") message.state = "running";
      if (message && event.event_type === "run.completed") message.state = "completed";
      if (message && ["run.failed", "run.unknown"].includes(event.event_type))
        message.state = "failed";
      if (message && event.event_type === "run.interrupted")
        message.state = "cancelled";
      if (event.event_type === "message.accepted"
          && conversation.state !== "running")
        conversation.state = "queued";
      if (event.event_type === "run.started") conversation.state = "running";
      if (["permission.requested", "input.requested"].includes(event.event_type))
        conversation.state = "waiting";
      if (["run.completed", "run.interrupted"].includes(event.event_type))
        conversation.state = chatQueuedCount(messages) ? "queued" : "idle";
      if (["run.failed", "run.unknown"].includes(event.event_type))
        conversation.state = "error";
      paint();
      if (["message.accepted", "run.completed", "run.failed",
           "run.interrupted", "run.unknown",
           "conversation.renamed", "conversation.closed"].includes(event.event_type))
        refresh();
    },
    (value) => {
      streamState.textContent = value;
      streamState.classList.toggle("reconnecting", value === "reconnecting");
    },
  );
}

async function renderInterface(root) {
  chatStopStream();
  const generation = ++chatRenderGeneration;
  root.replaceChildren(el("div", { className: "chat-loading" }, "Loading conversations…"));
  const [{ shells }, defaults, catalog, allConversations] = await Promise.all([
    api("/shells"),
    api("/flavor-defaults"),
    api("/models").catch(() => ({ harnesses: {}, stale: true })),
    chatApi("/conversations?limit=100"),
  ]);
  if (generation !== chatRenderGeneration) return;
  if (!shells.length) {
    root.replaceChildren(el("div", { className: "card muted" }, "No shells."));
    return;
  }
  const openConversation = allConversations.items.find(
    (item) => item.state !== "closed");
  const shell = shells.find((item) => item.shortname === chatRouteShell)
    || (!chatRouteShell && openConversation
      ? shells.find((item) => item.shell_id === openConversation.shell.shell_id)
      : null)
    || shells[0];
  const conversations = allConversations.items.filter(
    (item) => item.shell.shell_id === shell.shell_id);
  const configuring = chatRouteConversation === CHAT_CONFIGURE_ROUTE;
  let selectedId = configuring
    ? ""
    : chatRouteConversation
      || conversations.find((item) => item.state !== "closed")?.conversation_id
      || "";
  if (selectedId && !conversations.some((item) => item.conversation_id === selectedId))
    selectedId = "";
  const selectedConversation = allConversations.items.find(
    (item) => item.conversation_id === selectedId);

  const layout = el("div", { className: "chat-layout" });
  const rail = el("aside", { className: "chat-rail" });
  rail.append(el("div", { className: "chat-rail-title" }, "Shells"));
  const orderedFlavors = [
    ...CHAT_FLAVOR_ORDER.filter((flavor) => shells.some((item) => item.flavor === flavor)),
    ...[...new Set(shells.map((item) => item.flavor || "bespoke"))]
      .filter((flavor) => !CHAT_FLAVOR_ORDER.includes(flavor)).sort(),
  ];
  for (const flavor of orderedFlavors) {
    rail.append(el("div", { className: "chat-shell-group" }, flavor || "bespoke"));
    for (const item of shells.filter((candidate) => (candidate.flavor || "bespoke") === flavor)) {
      const active = allConversations.items.find(
        (conversation) => conversation.shell.shell_id === item.shell_id
          && conversation.state !== "closed");
      const button = el("button", {
        className: "chat-shell"
          + (item.shell_id === shell.shell_id ? " selected" : "")
          + (active ? " active-chat" : ""),
        type: "button",
      },
      el("span", { className: "chat-shell-name" }, item.display_name),
      el("span", { className: "chat-shell-shortname" }, item.shortname || ""));
      button.onclick = () => {
        if (item.shell_id === shell.shell_id) return;
        location.hash = chatHash(item.shortname);
      };
      rail.append(button);
    }
  }

  const side = el("aside", { className: "chat-history" });
  const newChat = el("button", { className: "act primary", type: "button", textContent: "＋ New chat" });
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
      newChat.textContent = "＋ New chat";
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
    el("div", { className: "chat-history-shell" }, el("b", {}, shell.display_name),
      el("span", { className: "chat-shortname" }, ` /${shell.shortname}`)),
    el("div", { className: "chat-history-actions" }, newChat, configure)));
  const history = el("div", { className: "chat-history-list" });
  for (const conversation of conversations) {
    const button = el("button", {
      className: "chat-history-item"
        + (conversation.conversation_id === selectedId ? " selected" : ""),
      type: "button",
    });
    button.append(el("span", { className: "chat-history-context" },
      `${chatStartedLabel(conversation)} | ${chatModelLabel(conversation)}`),
      el("span", { className: "chat-history-name" },
      chatConversationName(conversation)),
      chatStatePill(conversation.state));
    button.onclick = async () => {
      if (conversation.conversation_id === selectedId) return;
      if (await chatCloseForSwitch(selectedConversation))
        location.hash = chatHash(shell.shortname, conversation.conversation_id);
    };
    history.append(button);
  }
  if (!conversations.length)
    history.append(el("div", { className: "chat-history-empty" }, "No chats yet."));
  side.append(history);

  const pane = el("section", { className: "chat-pane" });
  layout.append(rail, side, pane);
  root.replaceChildren(layout);
  if (!selectedId) {
    if (configuring) {
      await chatRenderNew(pane, shell, defaults, catalog);
    } else {
      pane.append(el("div", { className: "chat-empty chat-no-selection" },
        "No chat selected."));
    }
    return;
  }
  const [conversation, messagePage] = await Promise.all([
    chatApi(`/conversations/${selectedId}`),
    chatApi(`/conversations/${selectedId}/messages?limit=100`),
  ]);
  if (generation !== chatRenderGeneration) return;
  await chatRenderOpen(pane, conversation, messagePage.items);
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
  const label = tab === "sprints" ? "Sprints"
    : document.querySelector(`nav button[data-tab="${tab}"]`)?.textContent
      || tab;
  document.title = forkName ? `${label} · ${forkName}` : label;
}
function show(tab) {
  for (const b of document.querySelectorAll("nav button")) b.classList.toggle("active", b.dataset.tab === tab);
  for (const k of Object.keys(VIEWS)) $(VIEWS[k][0]).hidden = k !== tab;
  document.body.classList.toggle("sprints-view", tab === "sprints");
  document.body.classList.toggle("interface-view", tab === "interface");
  if (tab !== "sprints") sprintsStopRender();
  if (tab !== "interface") chatStopStream();
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
    const [, shell = "", conversation = ""] = raw.split("/");
    chatRouteShell = decodeURIComponent(shell);
    chatRouteConversation = decodeURIComponent(conversation);
    show("interface");
    return;
  }
  if (raw === "" || raw === "shells" || raw.startsWith("shells-")) {
    shellTab = Object.entries(SHELL_TAB_HASH).find(([, hash]) => hash === raw)?.[0]
      || "harness";
    show("shells");
    return;
  }
  if (raw === "roadmap" || raw.startsWith("roadmap-")) {
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
  // The active count is live global navigation state. Start its document-wide
  // poll and establish the first value before routing paints any tab.
  try {
    sprintsStartPoll();
    await sprintsRefresh({ render: false });
  } catch {}
  routeFromHash();   // honor #tab on load (refresh / deep link), else Shells
})();
