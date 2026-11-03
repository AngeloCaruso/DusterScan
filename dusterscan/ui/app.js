"use strict";

const $ = (id) => document.getElementById(id);
let api = null;
let state = {};
let S = {};          // UI strings for the current language ("ui.*")
let conn = null;     // last successful connection info
let scanSummary = null;
let aiTimer = null;
let running = false;  // a scan, AI analysis, battery test or module re-read is in progress
// Live data view: tiles by value id, each with its recent history for the sparkline.
// source: "engine" (OBD-II) or a module's CAN id; group: the module screen shown.
const live = { running: false, open: false, tiles: {}, speed: null, last: null, period: null, state: null,
               source: "engine", group: null, sources: [] };
const SPARK_POINTS = 60;
// Module definitions card (Renault database)
const modules = { rows: [], reading: null };
const TIER_TONE = { exact: "green", same_software: "green", same_family: "amber", same_address: "zinc" };
// Battery & charging test panel
const batt = { open: false, running: false, step: null, instruction: null, button: null, v: null, min: null,
               result: null, error: null };
const BATT_STEPS = ["rest", "crank", "idle", "load"];

// -- translation ---------------------------------------------------------------

function T(key, params) {
  let s = S[key] ?? key;
  for (const [k, v] of Object.entries(params || {})) s = s.replaceAll(`{${k}}`, v);
  return s;
}

function applyStrings() {
  document.documentElement.lang = state.language;
  for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = T(el.dataset.i18n);
  for (const el of document.querySelectorAll("[data-i18n-html]")) el.innerHTML = T(el.dataset.i18nHtml);
  for (const el of document.querySelectorAll("[data-i18n-placeholder]")) el.placeholder = T(el.dataset.i18nPlaceholder);
  for (const el of document.querySelectorAll("[data-i18n-title]")) el.title = T(el.dataset.i18nTitle);
  // Status lines written from a key are re-translated; texts that came from Python stay as they are.
  for (const el of document.querySelectorAll(".status")) if (el._msg) setStatus(el, el._msg.key, el._msg.params, el._msg.kind);
  renderConnection();
  renderScanResult();
}

// -- status lines ----------------------------------------------------------------

function show(el, text, kind) {
  el.textContent = text || "";
  el.classList.remove("ok", "err");
  if (kind) el.classList.add(kind);
  // Single-line status lines cut long text with an ellipsis; the full text stays in the tooltip.
  if (el.classList.contains("truncate")) el.title = text || "";
}

function setStatus(el, key, params, kind) {
  el._msg = { key, params, kind };
  show(el, T(key, params), kind);
}

function setRaw(el, text, kind) {      // text already translated by Python
  el._msg = null;
  show(el, text, kind);
}

function setBusy(btn, on) {
  btn.disabled = on;
  if (on) btn.setAttribute("aria-busy", "true");
  else btn.removeAttribute("aria-busy");
}

// -- rendering helpers ------------------------------------------------------------

function renderConnection() {
  const online = !!conn;
  const st = $("conn-state");
  st.classList.toggle("ok", online);
  st.querySelector(".dot").classList.toggle("ok", online);
  const label = st.querySelector("[data-i18n]");
  label.dataset.i18n = online ? "ui.online" : "ui.offline";
  label.textContent = T(label.dataset.i18n);
  $("readout").classList.toggle("hidden", !online);
  $("hdr-vehicle-dot").classList.toggle("ok", online);
  renderLive();
  if (!online) return;
  $("ro-connected").textContent = T("ui.connected", { version: conn.version, port: conn.port });
  $("ro-protocol").textContent = conn.protocol;
  $("ro-protocol-line").title = conn.protocol;
  $("ro-voltage").textContent = conn.voltage != null ? volts(conn.voltage) : "–";
}

function renderScanResult() {
  $("scan-result").classList.toggle("hidden", !scanSummary);
  if (scanSummary) {
    const text = T("ui.scan_done", { codes: scanSummary.total_fault_codes, modules: scanSummary.modules_responding });
    $("scan-result-text").textContent = text;
    $("scan-result").title = text;
  }
}

function showAutosave(path) {
  if (!path) return;
  $("autosave").classList.remove("hidden");
  $("autosave-path").textContent = path;
  $("autosave-path").title = path;
}

async function refreshPorts() {
  const sel = $("port");
  const ports = await api.list_ports();
  const keep = sel.value;
  sel.innerHTML = "";
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = T("ui.auto_detect");
  sel.append(auto);
  for (const p of ports) {
    const o = document.createElement("option");
    o.value = p.device;
    o.textContent = `${p.device} - ${p.description}${p.likely ? ` (${T("ui.likely_adapter")})` : ""}`;
    sel.append(o);
  }
  sel.value = keep;
  $("port-hint").classList.toggle("hidden", ports.length > 0);
}

function updateEmpty() {
  $("empty").classList.toggle("hidden", live.open || batt.open || !!$("report").innerHTML);
}

async function showReport() {
  const html = await api.get_report_html();
  $("report").innerHTML = html;
  updateEmpty();
  for (const id of ["btn-ai", "btn-save-html", "btn-save-json"]) $(id).disabled = !html;

  const s = html ? await api.get_report_summary() : null;
  $("hdr-vehicle").classList.toggle("hidden", !s);
  $("hdr-status").classList.toggle("hidden", !s);
  if (!s) return;
  $("hdr-vehicle-name").textContent = s.vehicle;
  const pill = $("hdr-status");
  pill.classList.remove("ok", "warn", "bad");
  pill.classList.add({ good: "ok", attention: "warn" }[s.status] || "bad");
  $("hdr-status-text").textContent = s.label;
}

async function changeLanguage(lang) {
  state = await api.set_language(lang);
  S = state.strings;
  applyStrings();
  await refreshPorts();
  if (state.has_report) await showReport();
  await renderModules();
  renderBattery();
}

// -- actions -----------------------------------------------------------------------

async function connect() {
  const btn = $("btn-connect");
  setBusy(btn, true);
  conn = null;
  renderConnection();
  setStatus($("connect-status"), $("demo").checked ? "ui.connecting_demo" : "ui.connecting");
  const r = await api.connect($("port").value, $("demo").checked);
  setBusy(btn, false);
  if (!r.ok) {
    setRaw($("connect-status"), r.error, "err");
    $("btn-scan").disabled = true;
    state.connected = false;
    return;
  }
  setRaw($("connect-status"), "");
  conn = r;
  state.connected = true;
  renderConnection();
  $("btn-scan").disabled = false;
}

function lockWhileRunning(on) {
  running = on;
  $("btn-connect").disabled = on;
  $("language").disabled = on || live.running;
  for (const r of document.querySelectorAll('input[name="scantype"]')) r.disabled = on;
  renderLive();
}

// -- live data ------------------------------------------------------------------------

function renderLive() {
  // Anything else using the adapter (scan, battery test, module re-read) blocks live data.
  const scanning = (running && $("btn-scan").getAttribute("aria-busy") === "true") || batt.running || !!modules.reading;
  $("conn-tools").classList.toggle("hidden", !conn);
  $("btn-live").disabled = scanning;
  $("btn-battery").disabled = scanning || live.running || running;
  $("btn-live-label").textContent = T(live.running ? "ui.live_stop" : "ui.live_data");
  $("btn-live-toggle-label").textContent = T(live.running ? "ui.live_pause" : "ui.live_resume");
  $("btn-live-toggle").disabled = scanning || (!live.running && !conn);
  $("live").classList.toggle("hidden", !live.open);
  $("live").classList.toggle("stopped", !live.running);
  const s = live.state || {};
  const st = $("live-state");
  st.classList.toggle("on", s.kind === "on");
  st.classList.toggle("err", s.kind === "err");
  $("live-state-text").textContent = s.raw ?? (s.key ? T(s.key, s.params) : "");
  updateEmpty();
}

async function toggleLive() {
  if (live.running) {
    await api.stop_live();  // the "live_stopped" event updates the view
    return;
  }
  await renderLiveSources();
  await startLive();
  $("content").scrollTop = 0;
}

async function startLive() {
  const r = await api.start_live(live.source, live.group);
  if (!r.ok) { setRaw($("connect-status"), r.error, "err"); return; }
  live.group = r.group;
  Object.assign(live, { running: true, open: true, tiles: {}, speed: null, last: null, period: null,
                        state: { key: "ui.live_starting" } });
  batt.open = batt.running ? batt.open : false;
  $("batt").classList.toggle("hidden", !batt.open);
  $("live-grid").innerHTML = "";
  $("language").disabled = true;  // tile names come translated from Python
  $("live-note").textContent = T(live.source === "engine" ? "ui.live_note" : "ui.live_note_module");
  renderLive();
  renderLiveSources(false);
}

// Source (engine / module) and module screen pickers, shown when a scan matched module definitions.
async function renderLiveSources(fetch = true) {
  if (fetch) live.sources = await api.live_sources();
  const sources = live.sources;
  $("live-src").classList.toggle("hidden", sources.length < 2);
  let src = sources.find((s) => s.id === live.source);
  if (!src) { src = sources[0]; live.source = "engine"; live.group = null; }
  const slot = $("live-source");
  slot.innerHTML = "";
  const btn = comboButton(src.label);
  btn.onclick = () => openCombo({
    anchor: btn, current: live.source,
    load: async (q) => ({ groups: [{ label: "", items: filterItems(sources.map((s) => ({ value: s.id, label: s.label })), q) }] }),
    onPick: (id) => { if (id === live.source) return; live.source = id; live.group = null; startLive(); },
  });
  slot.append(btn);
  const gslot = $("live-group");
  gslot.innerHTML = "";
  gslot.classList.toggle("hidden", !src.groups.length);
  if (src.groups.length) {
    const gbtn = comboButton(live.group || src.groups[0]);
    gbtn.onclick = () => openCombo({
      anchor: gbtn, current: live.group,
      load: async (q) => ({ groups: [{ label: "", items: filterItems(src.groups.map((g) => ({ value: g, label: g })), q) }] }),
      onPick: (g) => { if (g === live.group) return; live.group = g; startLive(); },
    });
    gslot.append(gbtn);
  }
}

function filterItems(items, query) {
  const words = (query || "").toLowerCase().split(/\s+/).filter(Boolean);
  return items.filter((it) => words.every((w) => it.label.toLowerCase().includes(w)));
}

// -- searchable dropdown --------------------------------------------------------------
// One open at a time. opts: { anchor, current, allToggle, onPick(value),
//   load(query, all) -> Promise<{ groups: [{ label, items: [{ value, label, meta, badge, tone }] }], more }> }

const combo = { pop: null, opts: null, flat: [], active: -1, all: false, seq: 0 };

function comboButton(label) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "combo-btn";
  b.setAttribute("aria-haspopup", "listbox");
  b.setAttribute("aria-expanded", "false");
  b.innerHTML = '<span class="combo-val"></span><svg class="ic ic-sm"><use href="#i-chevron"/></svg>';
  b.querySelector(".combo-val").textContent = b.title = label;
  return b;
}

function openCombo(opts) {
  closeCombo();
  Object.assign(combo, { opts, all: false, active: -1, flat: [] });
  const pop = document.createElement("div");
  pop.className = "combo-pop";
  pop.innerHTML = '<div class="combo-search"><svg class="ic ic-sm"><use href="#i-search"/></svg>' +
    '<input type="text" role="combobox" aria-expanded="true" aria-autocomplete="list" aria-controls="combo-list" ' +
    'autocomplete="off" spellcheck="false"></div>' +
    (opts.allToggle ? '<label class="check combo-all"><input type="checkbox"> <span></span></label>' : "") +
    '<ul class="combo-list" id="combo-list" role="listbox"></ul><div class="combo-more hidden"></div>';
  const input = pop.querySelector("input");
  input.placeholder = T("ui.combo_search");
  if (opts.allToggle) {
    pop.querySelector(".combo-all span").textContent = T("ui.combo_all_vehicles");
    pop.querySelector(".combo-all input").onchange = (e) => { combo.all = e.target.checked; refreshCombo(input.value); };
  }
  document.body.append(pop);
  combo.pop = pop;
  placeCombo();
  opts.anchor.setAttribute("aria-expanded", "true");
  let timer = null;
  input.oninput = () => { clearTimeout(timer); timer = setTimeout(() => refreshCombo(input.value), 150); };
  input.onkeydown = comboKey;
  document.addEventListener("mousedown", comboOutside, true);
  refreshCombo("");
  input.focus();
}

function closeCombo() {
  if (!combo.pop) return;
  combo.pop.remove();
  combo.pop = null;
  combo.opts.anchor.setAttribute("aria-expanded", "false");
  document.removeEventListener("mousedown", comboOutside, true);
}

function comboOutside(ev) {
  if (combo.pop && !combo.pop.contains(ev.target) && !combo.opts.anchor.contains(ev.target)) closeCombo();
}

function placeCombo() {
  const r = combo.opts.anchor.getBoundingClientRect();
  const pop = combo.pop;
  const width = Math.min(Math.max(r.width, 320), window.innerWidth - 16);
  pop.style.width = width + "px";
  pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8)) + "px";
  const below = window.innerHeight - r.bottom;
  if (below < 260 && r.top > below) {
    Object.assign(pop.style, { top: "auto", bottom: (window.innerHeight - r.top + 4) + "px",
                               maxHeight: Math.min(400, r.top - 16) + "px" });
  } else {
    Object.assign(pop.style, { bottom: "auto", top: (r.bottom + 4) + "px", maxHeight: Math.min(400, below - 16) + "px" });
  }
}

async function refreshCombo(query) {
  const seq = ++combo.seq;
  const data = await combo.opts.load(query, combo.all);
  if (seq !== combo.seq || !combo.pop) return;  // a newer search already answered, or closed
  const list = combo.pop.querySelector(".combo-list");
  list.innerHTML = "";
  combo.flat = [];
  for (const g of data.groups) {
    if (!g.items.length) continue;
    if (g.label) {
      const h = document.createElement("li");
      h.className = "combo-group";
      h.setAttribute("role", "presentation");
      h.textContent = g.label;
      list.append(h);
    }
    for (const it of g.items) {
      const idx = combo.flat.length;
      const li = document.createElement("li");
      li.className = "combo-opt";
      li.id = `combo-opt-${idx}`;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(it.value === combo.opts.current));
      li.innerHTML = '<span class="opt-check"><svg class="ic ic-sm"><use href="#i-check"/></svg></span>' +
        '<span class="opt-text"><span class="opt-label"></span><span class="opt-meta"></span></span>';
      li.querySelector(".opt-label").textContent = li.title = it.label;
      li.querySelector(".opt-meta").textContent = it.meta || "";
      if (it.badge) {
        const b = document.createElement("span");
        b.className = `badge t-${it.tone || "zinc"}`;
        b.textContent = it.badge;
        li.append(b);
      }
      li.onmousemove = () => setComboActive(idx, false);
      li.onclick = () => pickCombo(idx);
      list.append(li);
      combo.flat.push({ value: it.value, el: li });
    }
  }
  if (!combo.flat.length) {
    const e = document.createElement("li");
    e.className = "combo-empty";
    e.textContent = T("ui.combo_none");
    list.append(e);
  }
  const more = combo.pop.querySelector(".combo-more");
  more.classList.toggle("hidden", !data.more);
  if (data.more) more.textContent = T("ui.combo_more", { n: data.more });
  setComboActive(Math.max(0, combo.flat.findIndex((f) => f.value === combo.opts.current)), true);
}

function setComboActive(i, scroll) {
  if (!combo.flat.length) return;
  combo.active = (i + combo.flat.length) % combo.flat.length;
  combo.flat.forEach((f, j) => f.el.classList.toggle("active", j === combo.active));
  const el = combo.flat[combo.active].el;
  combo.pop.querySelector("input").setAttribute("aria-activedescendant", el.id);
  if (scroll) el.scrollIntoView({ block: "nearest" });
}

function comboKey(ev) {
  if (ev.key === "ArrowDown") { ev.preventDefault(); setComboActive(combo.active + 1, true); }
  else if (ev.key === "ArrowUp") { ev.preventDefault(); setComboActive(combo.active - 1, true); }
  else if (ev.key === "Enter") { ev.preventDefault(); if (combo.active >= 0 && combo.flat.length) pickCombo(combo.active); }
  else if (ev.key === "Escape") { ev.preventDefault(); const a = combo.opts.anchor; closeCombo(); a.focus(); }
  else if (ev.key === "Tab") closeCombo();
}

function pickCombo(i) {
  const { value } = combo.flat[i];
  const { onPick, anchor } = combo.opts;
  closeCombo();
  anchor.focus();
  onPick(value);
}

// -- module definitions (Renault database) ----------------------------------------------

async function renderModules() {
  const m = await api.ddt_modules();
  $("step-modules").classList.toggle("hidden", !m.available || !m.rows.length);
  if (!m.available) return;
  modules.rows = m.rows;
  $("modules-state").textContent = T("ui.modules_matched", { n: m.used, total: m.rows.length });
  const list = $("mod-list");
  list.innerHTML = "";
  for (const row of m.rows) {
    const el = document.createElement("div");
    el.className = "mod-row";
    const head = document.createElement("div");
    head.className = "mod-head";
    const name = document.createElement("span");
    name.className = "mod-name";
    name.textContent = name.title = row.name;
    head.append(name, moduleBadge(row));
    const using = row.status === "used";
    const btn = comboButton(using ? row.ecuname : T("ui.def_generic"));
    btn.disabled = running;
    btn.onclick = () => openCombo({
      anchor: btn, allToggle: true, current: using ? row.file : "generic",
      load: (q, all) => loadDefinitions(row.tx, q, all),
      onPick: (v) => chooseDefinition(row, v),
    });
    el.append(head, btn);
    const note = row.tx === modules.reading ? T("ui.module_reading")
      : row.status === "rejected" ? T("ui.def_rejected_note") : "";
    if (note) {
      const n = document.createElement("div");
      n.className = "mod-note";
      n.textContent = note;
      el.append(n);
    }
    list.append(el);
  }
}

function moduleBadge(row) {
  const b = document.createElement("span");
  let key = "ui.tier_generic", tone = "zinc";
  if (row.status === "rejected") { key = "ui.def_rejected"; tone = "red"; }
  else if (row.status === "used" && row.chosen === "user") {
    key = "ui.def_chosen";
    tone = ["exact", "same_software"].includes(row.tier) ? "green" : "amber";
  } else if (row.status === "used") { key = `ui.tier_${row.tier}`; tone = TIER_TONE[row.tier] || "zinc"; }
  b.className = `badge t-${tone}`;
  b.textContent = T(key);
  return b;
}

async function loadDefinitions(tx, query, all) {
  const r = await api.ddt_candidates(tx, query, all);
  const day = (d) => (d ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : "");
  const opt = (it) => ({ value: it.file, label: it.ecuname, meta: day(it.date), badge: T(`ui.tier_${it.tier}`),
                         tone: TIER_TONE[it.tier] });
  return {
    groups: [
      { label: "", items: [{ value: "generic", label: T("ui.def_generic"), meta: T("ui.def_generic_meta") }] },
      { label: T("ui.combo_suggested"), items: r.items.filter((i) => i.tier !== "same_address").map(opt) },
      { label: T("ui.combo_others"), items: r.items.filter((i) => i.tier === "same_address").map(opt) },
    ],
    more: r.more,
  };
}

async function chooseDefinition(row, value) {
  const r = await api.ddt_choose(row.tx, value);
  if (!r.ok) { if (r.error) setRaw($("modules-status"), r.error, "err"); return; }
  setRaw($("modules-status"), "");
  if (r.reading) {  // the app re-reads the module with the new definition; "module_updated" ends it
    modules.reading = row.tx;
    lockWhileRunning(true);
  }
  await renderModules();
}

// -- battery & charging test ------------------------------------------------------------

async function startBattery() {
  const r = await api.start_battery();
  if (!r.ok) { setRaw($("connect-status"), r.error, "err"); return; }
  Object.assign(batt, { open: true, running: true, step: "rest", instruction: "ui.batt_instr_starting",
                        button: null, v: null, min: null, result: null, error: null });
  live.open = false;
  lockWhileRunning(true);
  renderBattery();
  $("content").scrollTop = 0;
}

const GRADE_TONE = { good: "green", ok: "green", fair: "amber", low: "red", weak: "red", high: "red" };

function renderBattery() {
  $("batt").classList.toggle("hidden", !batt.open);
  updateEmpty();
  if (!batt.open) return;
  const at = BATT_STEPS.indexOf(batt.step);
  for (const li of $("batt-steps").children) {
    const i = BATT_STEPS.indexOf(li.dataset.step);
    li.classList.toggle("done", !!batt.result || i < at);
    li.classList.toggle("active", !batt.result && i === at);
  }
  const st = $("batt-state");
  st.classList.toggle("on", batt.running);
  st.classList.toggle("err", !!batt.error);
  $("batt-state-text").textContent = batt.error || T(batt.running ? "ui.batt_running" : batt.result ? "ui.batt_done" : "ui.batt_stopped");
  $("btn-batt-cancel").classList.toggle("hidden", !batt.running);
  $("batt-now").classList.toggle("hidden", !batt.running);
  $("batt-instr").textContent = batt.instruction ? T(batt.instruction) : "";
  $("batt-v").textContent = batt.v != null ? local(batt.v.toFixed(2)) : "–";
  $("btn-batt-next").classList.toggle("hidden", !batt.button);
  if (batt.button) $("btn-batt-next-label").textContent = T(batt.button);
  const res = $("batt-result");
  res.classList.toggle("hidden", !batt.result);
  if (!batt.result) return;
  const r = batt.result;
  const tiles = [
    ["ui.batt_rest", r.rest_v, r.rest, ""],
    ["ui.batt_crank", r.crank_min_v, r.crank, r.brownout ? T("ui.batt_brownout") : r.crank_s ? T("ui.batt_seconds", { s: local(r.crank_s.toFixed(1)) }) : ""],
    ["ui.batt_idle", r.idle_v, r.idle, r.ecu_v ? T("ui.batt_ecu", { v: local(r.ecu_v.toFixed(1)) }) : ""],
    ["ui.batt_load", r.load_v, r.load, ""],
  ];
  res.innerHTML = "";
  for (const [label, v, grade, sub] of tiles) {
    const el = document.createElement("div");
    el.className = `bt t-${GRADE_TONE[grade] || "zinc"}`;
    el.innerHTML = '<span class="bt-label"></span><b class="bt-v"></b><span class="bt-grade"></span><span class="bt-sub"></span>';
    el.querySelector(".bt-label").textContent = T(label);
    el.querySelector(".bt-v").textContent = v != null ? volts(v, 2) : "–";
    el.querySelector(".bt-grade").textContent = grade ? T(`ui.batt_grade_${grade}`) : T("ui.batt_grade_none");
    el.querySelector(".bt-sub").textContent = sub;
    res.append(el);
  }
}

// -- Renault database (settings dialog) -------------------------------------------------

function renderDdt() {
  const d = state.ddt || {};
  $("ddt-status").textContent = d.imported
    ? T("ui.ddt_status", { n: Number(d.count).toLocaleString(state.language), date: (d.imported_at || "").slice(0, 10),
                           name: d.source_name })
    : T("ui.ddt_none");
  $("btn-ddt-import-label").textContent = T(d.imported ? "ui.ddt_replace" : "ui.ddt_import");
  $("btn-ddt-remove").classList.toggle("hidden", !d.imported);
  $("btn-ddt-import").disabled = !!d.importing;
  $("btn-ddt-remove").disabled = !!d.importing;
  $("ddt-progress").classList.toggle("hidden", !d.importing);
}

async function importDdt() {
  const r = await api.ddt_import();
  if (!r.ok) return;
  state.ddt = { ...(state.ddt || {}), importing: true };
  $("ddt-fill").style.width = "0";
  setStatus($("ddt-msg"), "ui.ddt_importing");
  renderDdt();
}

async function removeDdt() {
  state.ddt = await api.ddt_remove();
  setStatus($("ddt-msg"), "ui.ddt_removed", null, "ok");
  renderDdt();
  await renderModules();
}

// Spanish writes 12,4 V: decimal comma (Python does the same in reports, see i18n.DECIMAL_COMMA)
function local(s) { return state.language === "es" ? String(s).replace(".", ",") : String(s); }

function fmtNum(v) {
  if (typeof v !== "number" || Number.isInteger(v)) return String(v);
  return local(Math.abs(v) < 2 ? v.toFixed(3) : v.toFixed(1));
}

function volts(v, digits = 1) { return `${local(v.toFixed(digits))} V`; }

function buildLiveTiles(params) {
  const grid = $("live-grid");
  grid.innerHTML = "";
  live.tiles = {};
  for (const p of params) {
    const el = document.createElement("div");
    el.className = "lv";
    el.innerHTML = '<div class="lv-name"></div><div class="lv-val"><b>–</b><span></span></div>' +
      '<svg class="lv-spark" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true"><polyline/></svg>' +
      '<div class="lv-foot"><span class="lv-typ"></span><span class="lv-seen"></span></div>';
    el.querySelector(".lv-name").textContent = el.title = p.name;
    el.querySelector(".lv-val span").textContent = p.unit;
    if (p.normal) el.querySelector(".lv-typ").textContent = T("ui.live_typical", { range: `${p.normal[0]}–${p.normal[1]}` });
    grid.append(el);
    live.tiles[p.pid] = { p, el, hist: [], min: null, max: null };
  }
}

function drawSpark(tile) {
  const h = tile.hist;
  const line = tile.el.querySelector("polyline");
  if (h.length < 2) return line.setAttribute("points", "");
  let lo = Math.min(...h), hi = Math.max(...h);
  if (hi === lo) { lo -= 1; hi += 1; }
  const step = 100 / (SPARK_POINTS - 1);
  // newest point on the right edge
  line.setAttribute("points", h.map((v, i) =>
    `${(100 - (h.length - 1 - i) * step).toFixed(1)},${(22 - ((v - lo) / (hi - lo)) * 20).toFixed(1)}`).join(" "));
}

function updateLive(data) {
  const vals = data.values || {};
  if (typeof vals["0D"] === "number") live.speed = vals["0D"];
  for (const [pid, v] of Object.entries(vals)) {
    const tile = live.tiles[pid];
    if (!tile) continue;
    const b = tile.el.querySelector(".lv-val b");
    b.textContent = fmtNum(v);
    b.classList.toggle("txt", typeof v !== "number");
    if (typeof v !== "number") continue;
    tile.hist.push(v);
    if (tile.hist.length > SPARK_POINTS) tile.hist.shift();
    tile.min = tile.min == null ? v : Math.min(tile.min, v);
    tile.max = tile.max == null ? v : Math.max(tile.max, v);
    tile.el.querySelector(".lv-seen").textContent = T("ui.live_seen", { min: fmtNum(tile.min), max: fmtNum(tile.max) });
    drawSpark(tile);
    // Typical ranges are for idle, so they only mean something while the car is stopped.
    const n = tile.p.normal;
    tile.el.classList.toggle("warn", !!n && live.speed === 0 && (v < n[0] || v > n[1]));
  }
  if (data.voltage != null && conn) {
    conn.voltage = data.voltage;
    $("ro-voltage").textContent = volts(data.voltage);
  }
  if (live.last != null) {
    const dt = data.t - live.last;
    if (dt > 0) live.period = live.period == null ? dt : live.period * 0.7 + dt * 0.3;
  }
  live.last = data.t;
  live.state = { key: "ui.live_running", params: { s: live.period == null ? "…" : local(live.period.toFixed(1)) }, kind: "on" };
  renderLive();
}

async function startScan() {
  const deep = document.querySelector('input[name="scantype"]:checked').value === "deep";
  const r = await api.start_scan(deep);
  if (!r.ok) { setRaw($("scan-msg"), r.error, "err"); $("scan-progress").classList.remove("hidden"); return; }
  setBusy($("btn-scan"), true);
  live.open = false;  // the scan stopped live data; the report takes the space
  batt.open = false;
  renderBattery();
  lockWhileRunning(true);
  $("btn-ai").disabled = true;
  scanSummary = null;
  renderScanResult();
  $("step-scan").classList.remove("done");
  $("scan-progress").classList.remove("hidden");
  $("btn-cancel").classList.remove("hidden");
  $("btn-cancel").disabled = false;
  $("scan-fill").style.width = "0";
  setStatus($("scan-msg"), "ui.starting");
}

function scanFinished() {
  setBusy($("btn-scan"), false);
  lockWhileRunning(false);
  $("btn-ai").disabled = !state.has_report;
  $("btn-cancel").classList.add("hidden");
}

async function startAI() {
  const r = await api.start_ai($("symptoms").value);
  if (!r.ok) { setRaw($("ai-status"), r.error, "err"); if (!state.has_api_key) openSettings(); return; }
  setBusy($("btn-ai"), true);
  $("btn-scan").disabled = true;
  lockWhileRunning(true);
  const t0 = Date.now();
  const tick = () => setStatus($("ai-status"), "ui.analysing", { seconds: Math.round((Date.now() - t0) / 1000) });
  tick();
  aiTimer = setInterval(tick, 1000);
}

function aiFinished() {
  clearInterval(aiTimer);
  setBusy($("btn-ai"), false);
  $("btn-scan").disabled = !state.connected;
  lockWhileRunning(false);
}

// Copy buttons ([data-copy="#target"]) and report section tabs work by delegation,
// because the report HTML is inserted after the page loads.
document.addEventListener("click", async (ev) => {
  const copyBtn = ev.target.closest("[data-copy]");
  if (copyBtn) {
    const target = document.querySelector(copyBtn.dataset.copy);
    if (!target) return;
    try { await navigator.clipboard.writeText(target.innerText); } catch { return; }
    const label = copyBtn.querySelector(".copy-label");
    if (label) {
      const before = label.textContent;
      label.textContent = T("ui.copied");
      setTimeout(() => { label.textContent = before; }, 1500);
    } else {
      copyBtn.title = T("ui.copied");
      setTimeout(() => { copyBtn.title = T("ui.copy"); }, 1500);
    }
    return;
  }
  const tab = ev.target.closest(".ds-report .tab");
  if (tab) {
    for (const t of tab.parentElement.querySelectorAll(".tab")) t.classList.toggle("active", t === tab);
  }
});

// -- events pushed from Python ---------------------------------------------------

window.DS = {
  async on(event, data) {
    if (event === "progress") {
      $("scan-fill").style.width = Math.round(data.fraction * 100) + "%";
      setRaw($("scan-msg"), data.message);
    } else if (event === "scan_done") {
      scanFinished();
      state.has_report = true;
      $("step-scan").classList.add("done");
      $("scan-progress").classList.add("hidden");
      scanSummary = data.summary;
      renderScanResult();
      showAutosave(data.saved);
      await showReport();
      await renderModules();
      $("content").scrollTop = 0;
    } else if (event === "scan_failed") {
      scanFinished();
      setRaw($("scan-msg"), data.error, "err");
    } else if (event === "ai_done") {
      aiFinished();
      $("step-ai").classList.add("done");
      setStatus($("ai-status"), "ui.ai_done", { seconds: data.seconds }, "ok");
      showAutosave(data.saved);
      await showReport();
      $("content").scrollTop = 0;
    } else if (event === "ai_failed") {
      aiFinished();
      setRaw($("ai-status"), data.error, "err");
    } else if (event === "live_start") {
      live.running = true;  // also after switching source, which stops and restarts live data
      Object.assign(live, { last: null, period: null, speed: null });  // a new source starts its clock at 0
      buildLiveTiles(data.params);
      if (!data.params.length) live.state = { key: "ui.live_none", kind: "err" };
      renderLive();
    } else if (event === "live") {
      updateLive(data);
    } else if (event === "live_stopped") {
      live.running = false;
      if (data.error) live.state = { raw: data.error, kind: "err" };
      else if (live.state?.kind !== "err") live.state = { key: "ui.live_stopped" };
      $("language").disabled = running;
      renderLive();
    } else if (event === "module_updated") {
      if (modules.reading) { modules.reading = null; lockWhileRunning(false); }
      if (data.error) setRaw($("modules-status"), data.error, "err");
      await showReport();
      await renderModules();
    } else if (event === "ddt_progress") {
      $("ddt-fill").style.width = Math.round(data.fraction * 100) + "%";
    } else if (event === "ddt_done") {
      state.ddt = data.status;
      setStatus($("ddt-msg"), "ui.ddt_imported", null, "ok");
      renderDdt();
      if (state.has_report) await showReport();
      await renderModules();
    } else if (event === "ddt_failed") {
      state.ddt = { ...(state.ddt || {}), importing: false };
      setRaw($("ddt-msg"), data.error, "err");
      renderDdt();
    } else if (event === "battery_step") {
      Object.assign(batt, { step: data.step, instruction: data.instruction, button: data.button });
      if (data.step === "crank" && !data.button) batt.min = null;
      renderBattery();
    } else if (event === "battery_sample") {
      batt.v = data.v;
      renderBattery();
    } else if (event === "battery_done") {
      Object.assign(batt, { running: false, button: null, result: data.result, error: data.error });
      if (data.cancelled) batt.open = false;
      lockWhileRunning(false);
      renderBattery();
      if (data.result && state.has_report) await showReport();
    }
  },
};

function openSettings() {
  $("api-key").value = "";
  $("key-hint").textContent = T(state.has_api_key ? "ui.key_hint_saved" : "ui.key_hint_new");
  $("odometer").value = state.odometer_km ? Number(state.odometer_km).toLocaleString(state.language) : "";
  $("odometer-hint").textContent = state.odometer_km
    ? T("ui.odometer_hint_saved", { date: state.odometer_at || "" })
    : T("ui.odometer_hint_new");
  if (!state.ddt?.importing) setRaw($("ddt-msg"), "");
  renderDdt();
  $("settings").showModal();
}

async function init() {
  api = window.pywebview.api;
  state = await api.get_state();
  S = state.strings;
  $("version").textContent = "v" + state.version;

  const sel = $("language");
  for (const [code, name] of Object.entries(state.languages)) {
    const o = document.createElement("option");
    o.value = code;
    o.textContent = name;
    sel.append(o);
  }
  sel.value = state.language;
  sel.onchange = () => changeLanguage(sel.value);

  applyStrings();
  setStatus($("connect-status"), "ui.connect_hint");
  await refreshPorts();

  $("btn-refresh").onclick = refreshPorts;
  $("btn-connect").onclick = connect;
  $("btn-scan").onclick = startScan;
  $("btn-cancel").onclick = () => { api.cancel(); $("btn-cancel").disabled = true; setStatus($("scan-msg"), "ui.cancelling"); };
  $("btn-ai").onclick = startAI;
  $("btn-live").onclick = toggleLive;
  $("btn-live-toggle").onclick = toggleLive;
  $("btn-battery").onclick = startBattery;
  $("btn-batt-next").onclick = () => { batt.button = null; renderBattery(); api.battery_next(); };
  $("btn-batt-cancel").onclick = () => api.stop_battery();
  $("btn-batt-close").onclick = () => { if (batt.running) api.stop_battery(); batt.open = false; renderBattery(); };
  $("btn-ddt-import").onclick = importDdt;
  $("btn-ddt-remove").onclick = removeDdt;
  $("odometer").oninput = (e) => { e.target.value = e.target.value.replace(/[^\d.,\s]/g, ""); };
  // The dropdown is positioned over the page; it closes when what is under it moves.
  window.addEventListener("resize", closeCombo);
  document.addEventListener("scroll", (e) => { if (combo.pop && !combo.pop.contains(e.target)) closeCombo(); }, true);
  $("btn-live-close").onclick = async () => {
    if (live.running) await api.stop_live();
    live.open = false;
    renderLive();
  };
  $("btn-settings").onclick = openSettings;
  $("btn-save-html").onclick = async () => {
    const p = await api.save_html();
    if (p) setStatus($("files-status"), "ui.saved", { path: p }, "ok");
  };
  $("btn-save-json").onclick = async () => {
    const p = await api.save_json();
    if (p) setStatus($("files-status"), "ui.saved", { path: p }, "ok");
  };
  $("btn-open").onclick = async () => {
    const r = await api.open_saved();
    if (r.ok) {
      state.has_report = true;
      await showReport();
      await renderModules();
      setStatus($("files-status"), "ui.loaded", null, "ok");
    }
    else if (r.error) setRaw($("files-status"), r.error, "err");
  };
  $("btn-folder").onclick = () => api.open_reports_folder();

  // Ready: fade out the startup screen.
  $("boot").classList.add("done");
  setTimeout(() => $("boot").remove(), 400);
  $("settings").addEventListener("close", async () => {
    if ($("settings").returnValue !== "save") return;
    const key = $("api-key").value.trim();
    // empty key field = keep the saved key; empty odometer = forget it
    state = await api.save_settings(key || null, $("odometer").value.replace(/\D/g, ""));
  });
}

window.addEventListener("pywebviewready", init);
