// Cross-project Token Analytics for Subfloor Manager.
// Ported from the engine's Token Analytics sub-view (.super-coder/ui/app.js,
// anTokenSection) with a Project filter, project pills, and engine-update
// markers. Timestamps arrive UTC; all day-grouping is local, as upstream.

const $ = (s, r = document) => r.querySelector(s);
const el = (t, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(t), props);
  for (const k of kids) n.append(k?.nodeType ? k : document.createTextNode(k ?? ""));
  return n;
};
async function api(path, method = "GET") {
  const r = await fetch("/api/analytics" + path, { method });
  if (r.status === 401) {
    const next = encodeURIComponent(location.pathname + location.search + location.hash);
    location.replace("/signin.html?next=" + next);
  }
  const body = await r.json();
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}
function toast(msg) {
  const t = el("div", { className: "toast" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 4000);
}
const microlabel = (text) => el("span", { className: "microlabel" }, text);
function statRow(pairs) {
  const r = el("div", { className: "stat-row" });
  for (const [k, v] of pairs) r.append(el("span", { className: "stat" }, k + " ", el("b", {}, v)));
  return r;
}

let anFilters = { project: "", harness: "", model: "" };
let anSessions = [];
let anNextBefore = null;
let anDaysLoaded = 0;
let anClass = null;
let anUpdates = [];
let anActivity = [];  // chat turns + subagent runs, [start, end) — Shell Hours only

const AN_CLASSES = [
  ["input_tokens", "Input"], ["output_tokens", "Output"],
  ["cache_read_tokens", "Cache read"], ["cache_write_tokens", "Cache write"],
  ["reasoning_tokens", "Reasoning"],
];
const fmtTok = (n) => n == null ? "—"
  : n >= 1e9 ? (n / 1e9).toFixed(1) + "B"
  : n >= 1e6 ? (n / 1e6).toFixed(1) + "M"
  : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(Math.round(n));
const cardTotal = (c) => ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]
  .reduce((t, k) => t + (c[k] || 0), 0);
const localDay = (iso) => iso ? new Date(iso).toLocaleDateString(undefined,
  { weekday: "short", year: "numeric", month: "short", day: "numeric" }) : "undated";
const localTime = (iso) => iso ? new Date(iso).toLocaleTimeString(undefined,
  { hour: "2-digit", minute: "2-digit" }) : "—";
const ago = (iso) => {
  if (!iso) return "never";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m}m ago` : m < 1440 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
};

function anQuery(extra = {}) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...anFilters, ...extra })) if (v) p.set(k, v);
  const s = p.toString();
  return s ? "?" + s : "";
}

const AN_RANGES = [["1W", 7], ["1M", 30], ["3M", 90], ["6M", 180]];
let anRange = 7;
// "raw" = summed tokens; "avg" = tokens per shell (agent = project + shell,
// the same identity as Agents / day). Avg uses attributed sessions only —
// unattributed ones have no shell to divide by. "hours" = tokens per shell
// hour: tokens over the time shells actually worked (chat turns + subagent
// runs, summed across shells). See anHours.
const AN_MODES = [["Raw", "raw"], ["Av. / Shell", "avg"], ["Shell Hours", "hours"]];
let anMode = "raw";
const perShell = () => anMode === "avg";
const perHour = () => anMode === "hours";

async function anLoadPage(days) {
  const d = await api("/sessions" + anQuery({
    ...(anNextBefore ? { before: anNextBefore } : {}), days }));
  anSessions.push(...d.sessions);
  anNextBefore = d.next_before;
  anDaysLoaded += days;
}

const keyOf = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
function anBuckets(cls) {
  const days = anDaysLoaded || anRange;
  const buckets = [];
  const byKey = {};
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(Date.now() - i * 864e5);
    const b = { key: keyOf(d), date: d, value: 0, hours: 0, unattributed: 0,
                agents: new Set(), projects: new Set() };
    buckets.push(b);
    byKey[b.key] = b;
  }
  // an agent = one shell of one project; active on every local day its
  // sessions touch. Unattributed sessions have no identity and are counted
  // separately (b.unattributed), never guessed into an agent.
  for (const c of anSessions) {
    if (!c.started_at) continue;
    const end = new Date(c.ended_at || c.started_at);
    const d = new Date(c.started_at);
    d.setHours(12, 0, 0, 0);
    for (; d <= end || keyOf(d) === keyOf(end); d.setDate(d.getDate() + 1)) {
      const b = byKey[keyOf(d)];
      if (!b) continue;
      b.projects.add(c.project);
      if (c.unattributed) b.unattributed += 1; else b.agents.add(`${c.project}/${c.shell}`);
    }
  }
  if (cls === "agents") {
    for (const b of buckets) b.value = b.agents.size;
    return buckets;
  }
  if (perHour()) return anHours(buckets, byKey, cls);
  for (const c of anSessions) {
    if (!c.started_at || (perShell() && c.unattributed)) continue;
    const b = byKey[keyOf(new Date(c.started_at))];
    if (b) b.value += cls ? (c[cls] || 0) : cardTotal(c);
  }
  // per shell: the day's tokens over the agents active that day
  if (perShell()) for (const b of buckets) b.value = b.agents.size ? b.value / b.agents.size : 0;
  return buckets;
}

// split [s, e) at local midnights: fn(dayKey, hours) per day touched, so an
// interval crossing midnight — or the window's first day — counts only its share
function anSplitDays(s, e, fn) {
  const d = new Date(s);
  d.setHours(0, 0, 0, 0);
  while (d < e) {
    const next = new Date(d);
    next.setDate(next.getDate() + 1);
    fn(keyOf(d), (Math.min(e, next) - Math.max(s, d)) / 36e5);
    d.setTime(next.getTime());
  }
}

// Shell Hours. Tokens are recorded per harness session, hours per chat turn /
// subagent run, so they meet on project + shell: a session's tokens are spread
// over the days of its own shell's turns and runs inside the session, and the
// day's hours are those matched intervals (each counted once). Sessions with no
// such time (run outside the chat runtime, or with no shell) are left out, so
// the rate compares like with like. Hours follow the Project filter; the
// Harness/Model filters narrow the sessions and so the matched hours.
function anHours(buckets, byKey, cls) {
  const spans = {};
  for (const a of anActivity) {
    (spans[`${a.project}/${a.shell}`] ||= []).push(
      { s: new Date(a.started_at), e: new Date(a.ended_at), used: false });
  }
  for (const c of anSessions) {
    if (!c.started_at || !c.shell) continue;
    const s = new Date(c.started_at), e = new Date(c.ended_at || c.started_at);
    const share = {};
    let total = 0;
    for (const a of spans[`${c.project}/${c.shell}`] || []) {
      if (a.e <= s || a.s >= e) continue;
      a.used = true;
      anSplitDays(new Date(Math.max(a.s, s)), new Date(Math.min(a.e, e)), (k, h) => {
        share[k] = (share[k] || 0) + h;
        total += h;
      });
    }
    if (!total) continue;
    c.matched = true;
    const t = cls ? (c[cls] || 0) : cardTotal(c);
    for (const [k, h] of Object.entries(share)) if (byKey[k]) byKey[k].value += t * h / total;
  }
  for (const list of Object.values(spans)) for (const a of list) {
    if (a.used) anSplitDays(a.s, a.e, (k, h) => { if (byKey[k]) byKey[k].hours += h; });
  }
  for (const b of buckets) {
    b.tokens = b.value;
    b.value = b.hours ? b.value / b.hours : null;
  }
  return buckets;
}

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
  const cls = anClass === "agents" ? "Agents active per day —"
    : (anClass ? AN_CLASSES.find(([k]) => k === anClass)[1] : "Total") +
      (perShell() ? " tokens per shell —" : perHour() ? " tokens per shell hour —" : " tokens —");
  const scope = [anFilters.project || "all projects", anFilters.harness || "all harnesses",
                 anFilters.model].filter(Boolean).join(" · ");
  return `${cls} last ${anDaysLoaded || anRange} days — ${scope}`;
}

const SVG = "http://www.w3.org/2000/svg";
const svgEl = (t, attrs = {}) => {
  const n = document.createElementNS(SVG, t);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

function anChart(cls) {
  const buckets = anBuckets(cls);
  if (perHour() && cls !== "agents" && !buckets.some((b) => b.hours > 0)) {
    const empty = el("div", { className: "an-chart-wrap" });
    empty.append(el("div", { className: "an-chart-title" }, anChartLabel()),
      el("div", { className: "an-chart-empty", role: "status" },
        "No recorded shell activity in this window. Hourly rates are unavailable."));
    return empty;
  }
  const W = 860, H = 180, L = 48, R = 14, T = 10, B = 22;
  const iw = W - L - R, ih = H - T - B;
  const ymax = niceMax(Math.max(...buckets.map((b) => b.value)));
  const x = (i) => L + (buckets.length === 1 ? iw / 2 : (i / (buckets.length - 1)) * iw);
  const y = (v) => T + ih - (v / ymax) * ih;
  // Shell Hours: a day with no recorded hours has no rate — it is skipped (the
  // line joins its neighbours), never drawn as 0, which would read as "efficient"
  const on = buckets.map((b, i) => i).filter((i) => !perHour() || cls === "agents" || buckets[i].hours > 0);
  if (!on.length) on.push(buckets.length - 1);
  const pts = on.map((i) => [x(i), y(buckets[i].value)]);

  const wrap = el("div", { className: "an-chart-wrap", tabIndex: 0 });
  wrap.append(el("div", { className: "an-chart-title" }, anChartLabel()));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "an-chart" });
  for (const v of [0, ymax / 2, ymax]) {
    svg.append(svgEl("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "an-grid" }));
    const tick = svgEl("text", { x: L - 6, y: y(v) + 3, class: "an-tick", "text-anchor": "end" });
    tick.textContent = fmtTok(v);
    svg.append(tick);
  }
  const xLabel = (i, anchor) => {
    const t2 = svgEl("text", { x: x(i), y: H - 6, class: "an-tick", "text-anchor": anchor });
    t2.textContent = buckets[i].date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return t2;
  };
  svg.append(xLabel(0, "start"), xLabel(Math.floor(buckets.length / 2), "middle"),
             xLabel(buckets.length - 1, "end"));

  // engine updates: one dashed marker per local day,
  // hover for the refs and their commit subjects
  const idx = Object.fromEntries(buckets.map((b, i) => [b.key, i]));
  const marks = new Map();
  for (const u of anUpdates) {
    const i = idx[keyOf(new Date(u.landed_at))];
    if (i === undefined) continue;
    if (!marks.has(i)) marks.set(i, []);
    marks.get(i).push(u);
  }
  for (const [i, ups] of marks) {
    svg.append(svgEl("line", { x1: x(i), x2: x(i), y1: T, y2: T + ih, class: "ht-mark" }));
    const hit = svgEl("line", { x1: x(i), x2: x(i), y1: T, y2: T + ih, class: "ht-mark-hit" });
    const title = svgEl("title");
    title.textContent = "engine update\n" + ups.map((u) =>
      `${u.project}: ${u.ref} ${u.subject || ""}`).join("\n");
    hit.append(title);
    svg.append(hit);
  }

  const line = monotonePath(pts);
  svg.append(svgEl("path", { d: `${line}L${pts[pts.length - 1][0]},${y(0)}L${pts[0][0]},${y(0)}Z`, class: "an-area" }));
  svg.append(svgEl("path", { d: line, class: "an-line" }));

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
    const b = buckets[on[i]];
    const pad = (n) => String(n).padStart(2, "0");
    const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
    const count = cls === "agents" || perShell() ? plural(b.agents.size, "shell")
      : perHour() ? `${b.hours < 10 ? b.hours.toFixed(1) : Math.round(b.hours)} hours`
      : plural(b.projects.size, "project");
    const tok = cls === "agents" ? [] : [el("br"),
      `${fmtTok(b.value)} tok${perShell() ? " / shell" : perHour() ? " / hr" : ""}`];
    tip.replaceChildren(el("b", {}, `${pad(b.date.getMonth() + 1)}-${pad(b.date.getDate())}`),
      ...tok, el("br"), el("span", { className: "muted" }, count));
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
  const sel = el("select", { className: "an-select", ariaLabel: label });
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
  if (!anFilters.project) head.append(el("span", { className: "pill" }, c.project));
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
  const meta = [["project", c.project]];
  if (c.providers) meta.push(["provider", c.providers]);
  if (c.shell_session) meta.push(["session", c.shell_session]);
  if (c.status !== "ok") meta.push(["status", c.status]);
  body.append(statRow(meta));
  body.append(el("code", { className: "sess-ref" }, c.harness_session_ref));
  row.append(body);
  return row;
}

async function drawStatus() {
  const s = await api("/status");
  const errors = s.projects.filter((p) => p.error || p.warnings)
    .map((p) => p.project + ": " + (p.error || p.warnings));
  if (s.last_pull?.error) errors.unshift(s.last_pull.error);
  const partial = s.collection_state === "partial";
  const freshness = s.last_collection_at
    ? `collected ${ago(s.last_collection_at)}${partial ? " with warnings" : ""}`
    : s.projects.some((p) => p.usage_rows) ? "cached token history · collection incomplete" : "collected never";
  $("#status").textContent = `${s.projects.length} projects · ${freshness}`;
  $("#collection-errors").replaceChildren(...errors.map((error) => el("p", { className: "ht-errors" }, error)));
}

function setLoading(root, loading) {
  root.inert = loading;
  root.setAttribute("aria-busy", String(loading));
  $("#refresh").disabled = loading;
}

async function anTokenSection(root) {
  setLoading(root, true);
  const winDays = anDaysLoaded || anRange;
  const winFrom = new Date(Date.now() - winDays * 864e5).toISOString().slice(0, 10);
  let filters, usage;
  try {
    [filters, usage] = await Promise.all([
      api("/filters" + anQuery({ harness: "", model: "" })),
      api("/usage" + anQuery({ from: winFrom })),
      drawStatus()]);
    anUpdates = (await api("/engine-updates" + anQuery({ harness: "", model: "", from: winFrom }))).updates;
    const actFrom = new Date(Date.now() - (winDays - 1) * 864e5);
    actFrom.setHours(0, 0, 0, 0);   // the oldest chart bucket's local midnight
    anActivity = perHour() ? (await api("/activity" + anQuery({ harness: "", model: "",
      from: actFrom.toISOString().replace(/\.\d+Z$/, "Z") }))).activity : [];
    if (!anSessions.length && !anDaysLoaded) await anLoadPage(anRange);
  } catch (e) {
    root.replaceChildren(el("div", { className: "card" }, "error: " + e.message));
    return;
  } finally {
    setLoading(root, false);
  }
  root.replaceChildren();

  const reset = (k) => (v) => {
    anFilters[k] = v;
    if (k === "project") { anFilters.harness = ""; anFilters.model = ""; }
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
  // mode only re-renders: same rows, different arithmetic
  const modeSeg = el("div", { className: "filters seg an-range" });
  for (const [label, mode] of AN_MODES) {
    const chip = el("button", { className: "chip" + (anMode === mode ? " on" : ""),
      type: "button", textContent: label });
    chip.onclick = () => { anMode = mode; anTokenSection(root); };
    modeSeg.append(chip);
  }
  root.append(el("div", { className: "an-filters" },
    anSelect("Project", "project", filters.projects, reset("project")),
    anSelect("Harness", "harness", filters.harnesses, reset("harness")),
    anSelect("Model", "model", filters.models, reset("model")),
    el("div", { className: "ht-modes" }, rangeSeg, modeSeg)));

  const counted = perShell() ? anSessions.filter((c) => !c.unattributed) : anSessions;
  const shells = new Set(counted.map((c) => `${c.project}/${c.shell}`));
  const hoursTotal = perHour() ? anBuckets(null).reduce((t, b) => t + b.hours, 0) : 0;
  const matched = perHour() ? anSessions.filter((c) => c.matched).length : 0;
  const totals = {};
  for (const [k] of AN_CLASSES) {
    totals[k] = counted.reduce((t, c) => c[k] == null ? t : (t ?? 0) + c[k], null);
    if (perShell() && totals[k] != null) totals[k] = shells.size ? totals[k] / shells.size : 0;
    if (perHour() && totals[k] != null) totals[k] = hoursTotal
      ? anBuckets(k).reduce((t, b) => t + b.tokens, 0) / hoursTotal : null;
  }
  if (anClass && anClass !== "agents" && totals[anClass] == null) anClass = null;
  const agentDays = anBuckets("agents");
  const agentAvg = agentDays.reduce((t, b) => t + b.value, 0) / agentDays.length;
  const activeDays = agentDays.filter((b) => b.value);
  const graphCard = el("div", { className: "card an-graph" });
  const cardRow = el("div", { className: "an-cards" });
  const drawCards = () => {
    cardRow.replaceChildren();
    for (const [k, label] of AN_CLASSES) {
      if (k === "reasoning_tokens" && totals[k] == null) continue;
      const c = el("button", { className: "an-card" + (anClass === k ? " on" : ""), type: "button",
        ...(perShell() ? { title: `per shell — window total over ${shells.size} shell(s) (project + shell)` }
          : perHour() ? { title: `per shell hour — window total over ${hoursTotal.toFixed(1)} h of chat turns ` +
            `+ subagent runs; ${anSessions.length - matched} of ${anSessions.length} session(s) have no ` +
            `matching turn time and are left out` } : {}) });
      c.append(el("span", { className: "an-card-label" }, label),
               el("span", { className: "an-card-value" }, fmtTok(perHour() ? totals[k] : totals[k] ?? 0)));
      c.onclick = () => {
        anClass = anClass === k ? null : k;
        drawCards();
        graphCard.replaceChildren(anChart(anClass));
      };
      cardRow.append(c);
    }
    const a = el("button", { className: "an-card" + (anClass === "agents" ? " on" : ""), type: "button",
      title: `average distinct agents (project + shell) per day over all ${agentDays.length} days` +
        (activeDays.length ? ` · ${(activeDays.reduce((t, b) => t + b.value, 0) / activeDays.length).toFixed(1)}` +
          ` on the ${activeDays.length} active day(s) · peak ${Math.max(...agentDays.map((b) => b.value))}` : "") });
    a.append(el("span", { className: "an-card-label" }, "Agents / day"),
             el("span", { className: "an-card-value" }, agentAvg.toFixed(1)));
    a.onclick = () => {
      anClass = anClass === "agents" ? null : "agents";
      drawCards();
      graphCard.replaceChildren(anChart(anClass));
    };
    cardRow.append(a);
  };
  drawCards();
  graphCard.append(anChart(anClass));
  root.append(cardRow, graphCard);

  // a row's project prefix only when the view spans projects
  const pfx = (p) => anFilters.project ? "" : `${p} · `;
  const panelsTop = el("div", { className: "an-panels" });
  const panels = el("div", { className: "an-panels" });
  const panel = (label, valueText, items) => {
    const p = el("div", { className: "card an-panel" });
    p.append(microlabel(label), el("div", { className: "an-panel-value" }, valueText));
    for (const it of (items || []).slice(0, 5)) {
      const row = el("div", { className: "an-usage-row" });
      if (it && typeof it === "object") {
        const num = "#" + it.id;
        row.append(el("span", { className: it.ref ? "ht-ref" : "an-id" }, it.ref || num),
                   el("span", { className: "an-row-label" }, " " + (it.label || "")));
        row.title = `${it.ref || num} ${it.label || ""}`;
      } else {
        row.append(it);
      }
      p.append(row);
    }
    return p;
  };
  const favP = el("div", { className: "card an-panel" }, microlabel("Favorite model by flavor"));
  const favs = usage.favorite_models || [];
  if (!favs.length) favP.append(el("div", { className: "an-panel-value" }, "—"));
  for (const f of favs)
    favP.append(el("div", { className: "an-usage-row" },
      el("span", { className: "pill" }, f.flavor), " ", f.model,
      el("span", { className: "muted" }, ` — ${f.sessions} session(s)`)));
  panelsTop.append(favP);
  const peak = anBuckets(null).reduce((a, b) => (b.value > a.value ? b : a));
  panelsTop.append(panel(perShell() ? "Peak day / shell" : perHour() ? "Peak day / shell hr" : "Peak day", peak.value ? fmtTok(peak.value) : "—",
    peak.value ? [peak.date.toLocaleDateString(undefined,
      { weekday: "short", month: "short", day: "numeric" }) + " — all models"] : []));
  panelsTop.append(panel("Engine updates", String(anUpdates.length),
    anUpdates.map((u) => ({ ref: u.ref.slice(0, 8), label: `${pfx(u.project)}${u.subject || "(unknown locally)"}` }))));
  panels.append(panel("Features shipped", String((usage.features_shipped || []).length),
    (usage.features_shipped || []).map((f) => ({ id: f.feature_id, label: pfx(f.project) + f.title }))));
  panels.append(panel("Specs shipped", String((usage.specs_shipped || []).length),
    (usage.specs_shipped || []).map((s) => ({ id: s.document_id, label: pfx(s.project) + (s.title || s.feature_title) }))));
  panels.append(panel("Docs outstanding", String((usage.docs_outstanding || []).length),
    (usage.docs_outstanding || []).map((f) => ({ id: f.feature_id, label: pfx(f.project) + f.title }))));
  root.append(panelsTop, panels);

  const list = el("div", {});
  root.append(list);
  if (!anSessions.length) list.append(el("div", { className: "muted" }, "No sessions in the loaded window."));
  const byDay = new Map();
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
      setLoading(root, true);
      try { await anLoadPage(7); await anTokenSection(root); }
      catch (e) { toast("error: " + e.message); }
      finally { setLoading(root, false); }
    };
    list.append(el("div", { className: "an-more" }, more));
  }
}

const root = $("#view");
$("#refresh").onclick = async () => {
  setLoading(root, true);
  $("#status").textContent = "Collecting local usage…";
  try {
    const r = await api("/refresh", "POST");
    if (!r.ok) toast("pull failed: " + r.error);
    anSessions = []; anNextBefore = null; anDaysLoaded = 0;
    await anTokenSection(root);
  } catch (err) { toast("error: " + err.message); }
  setLoading(root, false);
};
root.append(el("div", { className: "muted" }, "loading…"));
anTokenSection(root);
