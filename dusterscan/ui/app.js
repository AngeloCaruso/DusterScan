"use strict";

const $ = (id) => document.getElementById(id);
let api = null;
let state = {};
let S = {};          // UI strings for the current language ("ui.*")
let conn = null;     // last successful connection info
let scanSummary = null;
let aiTimer = null;
let running = false;  // a scan or AI analysis is in progress
// Live data view: tiles by PID, each with its recent history for the sparkline.
const live = { running: false, open: false, tiles: {}, speed: null, last: null, period: null, state: null };
const SPARK_POINTS = 60;

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
  $("ro-voltage").textContent = conn.voltage != null ? `${conn.voltage.toFixed(1)} V` : "–";
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
  $("empty").classList.toggle("hidden", live.open || !!$("report").innerHTML);
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
  const scanning = running && $("btn-scan").getAttribute("aria-busy") === "true";
  $("btn-live").classList.toggle("hidden", !conn);
  $("btn-live").disabled = scanning;
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
  const r = await api.start_live();
  if (!r.ok) { setRaw($("connect-status"), r.error, "err"); return; }
  Object.assign(live, { running: true, open: true, tiles: {}, speed: null, last: null, period: null,
                        state: { key: "ui.live_starting" } });
  $("live-grid").innerHTML = "";
  $("language").disabled = true;  // tile names come translated from Python
  renderLive();
  $("content").scrollTop = 0;
}

function fmtNum(v) {
  if (typeof v !== "number" || Number.isInteger(v)) return String(v);
  return Math.abs(v) < 2 ? v.toFixed(3) : v.toFixed(1);
}

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
    $("ro-voltage").textContent = `${data.voltage.toFixed(1)} V`;
  }
  if (live.last != null) {
    const dt = data.t - live.last;
    live.period = live.period == null ? dt : live.period * 0.7 + dt * 0.3;
  }
  live.last = data.t;
  live.state = { key: "ui.live_running", params: { s: live.period == null ? "…" : live.period.toFixed(1) }, kind: "on" };
  renderLive();
}

async function startScan() {
  const deep = document.querySelector('input[name="scantype"]:checked').value === "deep";
  const r = await api.start_scan(deep);
  if (!r.ok) { setRaw($("scan-msg"), r.error, "err"); $("scan-progress").classList.remove("hidden"); return; }
  setBusy($("btn-scan"), true);
  live.open = false;  // the scan stopped live data; the report takes the space
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
    }
  },
};

function openSettings() {
  $("api-key").value = "";
  $("key-hint").textContent = T(state.has_api_key ? "ui.key_hint_saved" : "ui.key_hint_new");
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
    if (r.ok) { state.has_report = true; await showReport(); setStatus($("files-status"), "ui.loaded", null, "ok"); }
    else if (r.error) setRaw($("files-status"), r.error, "err");
  };
  $("btn-folder").onclick = () => api.open_reports_folder();

  // Ready: fade out the startup screen.
  $("boot").classList.add("done");
  setTimeout(() => $("boot").remove(), 400);
  $("settings").addEventListener("close", async () => {
    if ($("settings").returnValue !== "save") return;
    const key = $("api-key").value.trim();
    if (key) state = await api.save_settings(key);
  });
}

window.addEventListener("pywebviewready", init);
