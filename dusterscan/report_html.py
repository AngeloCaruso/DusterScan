"""Renders the scan report (and the AI analysis, if any) as HTML, in any language.

Visual language: the design team's "Telemetry Diagnostics" system (dark zinc
layers, hairline borders, monospaced data, colour only for diagnostic meaning).
Every value shown comes from the scan; nothing is decorative data.
"""

from __future__ import annotations

from functools import partial
from html import escape

from .dtc import describe
from .i18n import DECIMAL_COMMA, has, normalize, number, t, text
from .obd2 import PIDS
from .rules import ENGINE_TX, engine_direct, engine_direct_states

SEVERITIES = ["critical", "high", "medium", "low", "info"]
SEV_TONE = {"critical": "red", "high": "red", "medium": "amber", "low": "yellow", "info": "zinc"}
STATUS_TONE = {"good": "green", "attention": "amber", "problems": "red", "critical": "red"}
STATUS_ICON = {"good": "check-circle", "attention": "alert-triangle", "problems": "alert-octagon", "critical": "alert-octagon"}
DRIVE_TONE = {"yes": "green", "yes_with_caution": "amber", "no": "red"}
OBD_KIND_TONE = {"stored": "red", "pending": "amber", "permanent": "zinc"}
ECU_STATE_TONE = {"active": "red", "stored": "yellow", "pending": "amber", "failed_earlier": "zinc", "recorded": "zinc"}
GRADE_TONE = {"good": "green", "ok": "green", "fair": "amber", "low": "red", "weak": "red", "high": "red"}
TONE_RANK = ["zinc", "green", "amber", "red"]   # worst last
TIER_TONE = {"exact": "green", "same_software": "green", "same_family": "amber", "same_address": "zinc"}

# Lucide icons (ISC licence), inlined so saved reports work offline.
ICONS = {
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "alert-octagon": '<path d="M12 16h.01"/><path d="M12 8v4"/><path d="M15.312 2a2 2 0 0 1 1.414.586l4.688 4.688A2 2 0 0 1 22 8.688v6.624a2 2 0 0 1-.586 1.414l-4.688 4.688a2 2 0 0 1-1.414.586H8.688a2 2 0 0 1-1.414-.586l-4.688-4.688A2 2 0 0 1 2 15.312V8.688a2 2 0 0 1 .586-1.414l4.688-4.688A2 2 0 0 1 8.688 2z"/>',
    "alert-triangle": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "check-circle": '<circle cx="12" cy="12" r="10"/><path d="m16 9-5.5 5.5L8 12"/>',
    "activity": '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "sparkles": '<path d="M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z"/>',
}

CSS = """
.ds-report{--bg:#09090b;--card:rgb(24 24 27/.4);--card-2:rgb(9 9 11/.8);--border:#27272a;--border-2:#3f3f46;--row:rgb(39 39 42/.7);
  --fg-strong:#fafafa;--fg:#f4f4f5;--fg-2:#e4e4e7;--fg-3:#d4d4d8;--muted:#a1a1aa;--muted-2:#71717a;--faint:#52525b;
  --sans:"Inter","Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;--mono:"JetBrains Mono","Cascadia Mono","Cascadia Code",Consolas,monospace;
  font:14px/20px var(--sans);color:var(--fg);-webkit-font-smoothing:antialiased;display:flex;flex-direction:column;gap:24px}
.ds-report *{box-sizing:border-box}
.ds-report h1,.ds-report h2,.ds-report h3,.ds-report p{margin:0}
.ds-report .t-red{--tone:#f87171;--tone-strong:#ef4444;--tone-bg:rgb(239 68 68/.12);--tone-line:rgb(239 68 68/.3);--tone-card:rgb(127 29 29/.5)}
.ds-report .t-amber{--tone:#fbbf24;--tone-strong:#f59e0b;--tone-bg:rgb(245 158 11/.12);--tone-line:rgb(245 158 11/.3);--tone-card:rgb(120 53 15/.55)}
.ds-report .t-yellow{--tone:#facc15;--tone-strong:#eab308;--tone-bg:rgb(234 179 8/.12);--tone-line:rgb(234 179 8/.3);--tone-card:rgb(113 63 18/.55)}
.ds-report .t-green{--tone:#34d399;--tone-strong:#10b981;--tone-bg:rgb(16 185 129/.12);--tone-line:rgb(16 185 129/.3);--tone-card:rgb(6 78 59/.6)}
.ds-report .t-zinc{--tone:#d4d4d8;--tone-strong:#71717a;--tone-bg:#27272a;--tone-line:#3f3f46;--tone-card:#27272a}
.ds-report .ic{width:14px;height:14px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ds-report .mono{font-family:var(--mono)}
.ds-report .muted{color:var(--muted)}
.ds-report .dot{width:6px;height:6px;border-radius:50%;flex:none;background:var(--tone-strong,#52525b)}
.ds-report .pulse{animation:dsPulse 2s ease-in-out infinite}
@keyframes dsPulse{50%{opacity:.4}}
.ds-report .chip{display:inline-flex;align-items:center;gap:4px;height:20px;padding:0 8px;border-radius:4px;white-space:nowrap;
  font:600 10px/1 var(--mono);text-transform:uppercase;letter-spacing:.03em;background:var(--tone-bg);color:var(--tone);border:1px solid var(--tone-line)}
.ds-report .chip.plain{text-transform:none;letter-spacing:0}
.ds-report .count{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 8px;border-radius:999px;white-space:nowrap;
  font:500 11px/1 var(--mono);background:#18181b;border:1px solid var(--border);color:var(--muted);font-variant-numeric:tabular-nums}
.ds-report .count[class*="t-"]{background:var(--tone-bg);border-color:var(--tone-line);color:var(--tone)}
.ds-report .code{font-family:var(--mono);font-weight:700;color:var(--tone,var(--fg))}
/* hero */
.ds-report .hero{border:1px solid var(--border);background:rgb(24 24 27/.5);border-radius:8px;padding:20px;display:flex;flex-direction:column;gap:20px}
.ds-report .hero-top{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding-bottom:16px;border-bottom:1px solid rgb(39 39 42/.8)}
.ds-report h1{font-size:24px;line-height:32px;font-weight:600;letter-spacing:-.02em;color:var(--fg-strong)}
.ds-report .subline{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;margin-top:4px;font-size:12px;color:var(--muted)}
.ds-report .subline .sep{color:var(--faint)}
.ds-report .subline strong{color:var(--fg-2);font-weight:500}
.ds-report .subline .ts{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono)}
.ds-report .subline .ts .ic{width:12px;height:12px}
.ds-report .vin{font-family:var(--mono);color:var(--fg-3)}
.ds-report .vin code{padding:2px 6px;border-radius:4px;background:var(--bg);border:1px solid var(--border);color:var(--fg-2);font-weight:600;letter-spacing:.05em}
.ds-report .status-box{display:inline-flex;align-items:center;gap:12px;padding:8px 12px;border-radius:8px;border:1px solid var(--tone-line);background:var(--tone-bg)}
.ds-report .status-box .ic{width:20px;height:20px;color:var(--tone)}
.ds-report .status-box .sb-label{font:600 10px/14px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--tone)}
.ds-report .status-box .sb-text{font-size:12px;line-height:16px;font-weight:600;color:var(--fg)}
.ds-report .kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.ds-report .kpi{border:1px solid var(--border);background:var(--card-2);border-radius:8px;padding:16px;min-height:96px;display:flex;flex-direction:column;justify-content:space-between;gap:12px;min-width:0}
.ds-report .kpi[class*="t-"]{border-color:var(--tone-card)}
.ds-report .kpi-label{font-size:12px;font-weight:500;color:var(--muted)}
.ds-report .kpi-big{display:flex;align-items:baseline;gap:8px}
.ds-report .kpi-big b{font:700 24px/28px var(--mono);color:var(--tone,var(--fg));letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.ds-report .kpi-big span{font:12px var(--mono);color:var(--muted-2)}
.ds-report .kpi-val{font:500 12px/16px var(--mono);color:var(--fg-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-variant-numeric:tabular-nums}
.ds-report .kpi-val.toned{display:flex;align-items:center;gap:6px;color:var(--tone);font-weight:600}
.ds-report .kpi-sub{display:block;font:11px/16px var(--mono);color:var(--muted-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ds-report .kpi-row{display:flex;align-items:baseline;justify-content:space-between;gap:4px}
/* tabs */
.ds-report .tabs{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;border-bottom:1px solid var(--border);font-size:12px;font-weight:500;scrollbar-width:none;position:sticky;top:0;z-index:1;background:var(--bg)}
.ds-report .tab{display:inline-flex;align-items:center;gap:6px;height:30px;padding:0 12px;border-radius:6px;color:var(--muted);text-decoration:none;white-space:nowrap;transition:background-color .15s,color .15s}
.ds-report .tab:hover{background:#18181b;color:var(--fg-2)}
.ds-report .tab:focus-visible{outline:2px solid rgb(0 229 255/.6);outline-offset:-2px}
.ds-report .tab.active{background:var(--fg);color:#18181b;font-weight:600}
.ds-report .tab .count{height:18px;padding:0 6px;font-size:10px}
.ds-report .tab.active .count{background:var(--fg-2);color:#18181b;border-color:transparent}
/* sections */
.ds-report section{display:flex;flex-direction:column;gap:14px;scroll-margin-top:48px}
.ds-report .sec-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding-bottom:8px;border-bottom:1px solid var(--border)}
.ds-report .sec-title{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ds-report h2{font-size:16px;line-height:24px;font-weight:600;letter-spacing:-.01em;color:var(--fg)}
.ds-report .sec-meta{font:12px var(--mono);color:var(--muted);display:inline-flex;align-items:center;gap:6px}
.ds-report .sec-meta[class*="t-"]{color:var(--tone)}
.ds-report .empty{border:1px dashed var(--border);border-radius:8px;padding:16px;color:var(--muted);font-size:13px}
/* filters (CSS only: radio buttons + :has) */
.ds-report .filters{display:flex;flex-wrap:wrap;gap:6px}
.ds-report .filters input{position:absolute;opacity:0;pointer-events:none}
.ds-report .fbtn{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 10px;border-radius:6px;font-size:12px;font-weight:500;cursor:pointer;
  border:1px solid var(--tone-card,var(--border));background:var(--tone-bg,rgb(24 24 27/.6));color:var(--tone,var(--muted));transition:background-color .15s,box-shadow .15s}
.ds-report .fbtn:hover{filter:brightness(1.15)}
.ds-report .fbtn.all{background:rgb(24 24 27/.6);border-color:var(--border);color:var(--muted)}
.ds-report .filters input:checked+.fbtn{box-shadow:0 0 0 1px #a1a1aa}
.ds-report .filters input:checked+.fbtn.all{background:var(--fg);color:#18181b;border-color:transparent;box-shadow:none}
.ds-report .filters input:focus-visible+.fbtn{outline:2px solid rgb(0 229 255/.6);outline-offset:2px}
""" + "".join(
    f".ds-report:has(#sf-{s}:checked) .fcard:not(.sev-{s}){{display:none}}" for s in SEVERITIES
) + """
/* fault cards */
.ds-report .stack{display:flex;flex-direction:column;gap:10px}
.ds-report .fcard{position:relative;overflow:hidden;border-radius:8px;border:1px solid var(--tone-card);background:var(--card);padding:16px 16px 16px 24px;transition:border-color .15s}
.ds-report .fcard::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--tone-strong)}
.ds-report .fcard:hover{border-color:var(--tone-line)}
.ds-report .fcard-head{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:6px;font-size:12px}
.ds-report .fcard-head .sys{font-weight:500;color:var(--muted)}
.ds-report .fcard-head .sep{color:var(--faint)}
.ds-report .fcard h3{font-size:14px;line-height:20px;font-weight:600;letter-spacing:-.01em;color:var(--fg)}
.ds-report .fcard p{font-size:12px;line-height:20px;color:var(--muted);margin-top:4px}
.ds-report .fcard .evidence b{color:var(--fg-3);font-weight:600}
.ds-report .rows{margin-top:12px;border-top:1px solid var(--border)}
.ds-report .rows>div{display:grid;grid-template-columns:160px minmax(0,1fr);gap:16px;padding:10px 0;border-bottom:1px solid var(--row);font-size:12px;line-height:20px}
.ds-report .rows>div:last-child{border-bottom:0;padding-bottom:0}
.ds-report .rows dt{font:500 11px/20px var(--mono);text-transform:uppercase;letter-spacing:.04em;color:var(--muted-2)}
.ds-report .rows dd{margin:0;color:var(--fg-3)}
.ds-report ul,.ds-report ol{margin:0;padding-left:18px}
.ds-report li+li{margin-top:4px}
.ds-report .card{border-radius:8px;border:1px solid var(--border);background:var(--card);padding:16px}
.ds-report .card p+p{margin-top:8px}
.ds-report .card .chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.ds-report .card-list{font-size:13px;color:var(--fg-3)}
/* tables */
.ds-report .tbl{border-radius:8px;border:1px solid var(--border);background:var(--card);overflow:hidden}
.ds-report .tbl-scroll{overflow-x:auto}
.ds-report table{width:100%;border-collapse:collapse;font-size:12px;text-align:left}
.ds-report thead{background:var(--card-2)}
.ds-report th{padding:12px 16px;border-bottom:1px solid var(--border);font:500 11px/16px var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--muted);white-space:nowrap}
.ds-report th.num,.ds-report td.num{text-align:right}
.ds-report td{padding:12px 16px;border-top:1px solid var(--row);vertical-align:top;color:var(--fg-3);overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.ds-report tbody tr:first-child td{border-top:0}
.ds-report tbody tr{transition:background-color .15s}
.ds-report tbody tr:hover{background:rgb(24 24 27/.6)}
.ds-report td.strong{font-weight:500;color:var(--fg-2)}
.ds-report td.meaning{font-weight:500;color:var(--fg)}
.ds-report td .sub{display:block;font-size:11px;line-height:16px;color:var(--muted-2);margin-top:2px}
.ds-report td.state{font:11px/16px var(--mono);color:var(--muted)}
.ds-report td.state b{color:var(--tone);font-weight:600}
.ds-report tr.dim td{color:var(--muted-2)}
.ds-report td.nowrap{white-space:nowrap;overflow-wrap:normal}
.ds-report td.t-val{font-family:var(--mono);font-weight:600;color:var(--tone,var(--fg));white-space:nowrap}
.ds-report td.t-val .chip{margin-left:6px;height:18px;padding:0 4px}
.ds-report td.range{font-family:var(--mono);color:var(--muted-2);white-space:nowrap}
.ds-report tr.hot{background:rgb(69 10 10/.12)}
.ds-report tr.hot td:first-child{color:#fca5a5}
.ds-report .barcol{width:176px}
.ds-report .fillbar{height:6px;margin-top:7px;border-radius:999px;background:var(--border);overflow:hidden}
.ds-report .fillbar i{display:block;height:100%;border-radius:999px;background:var(--tone-strong,#10b981)}
.ds-report .metabar{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px 16px;padding:12px 16px;background:var(--card-2);border-top:1px solid var(--border);font:11px/16px var(--mono);color:var(--muted)}
.ds-report .metabar b{color:var(--fg-2);font-weight:500}
.ds-report .metabar span+span::before{content:"· ";color:var(--faint)}
.ds-report .toned{color:var(--tone);font-weight:600}
.ds-report .online{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;font-weight:500;color:var(--fg-2)}
.ds-report .online .dot{width:8px;height:8px}
/* readiness + freeze frame */
.ds-report .ready{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.ds-report .ready>div{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px;border-radius:8px;border:1px solid var(--tone-card);background:var(--card-2)}
.ds-report .ready>div.t-amber{background:rgb(69 26 3/.2)}
.ds-report .ready>div>span:first-child{font-size:12px;font-weight:500;color:var(--fg-3)}
.ds-report .ready>div.t-amber>span:first-child{color:#fcd34d}
.ds-report .note{font-size:12px;color:var(--muted-2)}
.ds-report .ff-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-wrap:wrap}
.ds-report h3.caps{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:-.01em;color:var(--fg-2)}
.ds-report .ff-desc{font-size:11px;line-height:16px;color:var(--muted);margin-top:2px}
.ds-report .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-top:12px}
.ds-report .tile{padding:10px;border-radius:6px;background:var(--bg);border:1px solid var(--border);min-width:0}
.ds-report .tile[class*="t-"]{background:var(--tone-bg);border-color:var(--tone-card)}
.ds-report .tile .tl{display:block;font:10px/14px var(--mono);text-transform:uppercase;color:var(--muted-2);overflow-wrap:anywhere}
.ds-report .tile[class*="t-"] .tl{color:var(--tone);font-weight:600}
.ds-report .tile .tv{display:block;margin-top:4px;font:600 12px/16px var(--mono);color:var(--tone,var(--fg));font-variant-numeric:tabular-nums}
.ds-report .tile .chip{margin-top:6px}
.ds-report .tile .tsub{display:block;margin-top:4px;font-size:11px;line-height:16px;color:var(--muted-2)}
.ds-report .mdata{display:flex;flex-direction:column;gap:8px;padding:16px;border-radius:8px;border:1px solid var(--border);background:var(--card)}
.ds-report .mdata+.mdata{margin-top:12px}
.ds-report .mdata-head{display:flex;flex-direction:column;gap:2px}
.ds-report .mdata-head b{color:var(--fg-strong);font-weight:600}
.ds-report .mdata-head .sub,.ds-report td .sub .chip{font-size:11px;color:var(--muted-2)}
.ds-report .mdata details{border-top:1px solid var(--border);padding-top:8px}
.ds-report .mdata summary{display:flex;align-items:center;gap:8px;cursor:pointer;font-size:12px;font-weight:500;color:var(--fg-3)}
.ds-report .mdata summary:focus-visible{outline:2px solid rgb(0 229 255/.6)}
.ds-report .mdata summary .count{height:18px;padding:0 6px;font-size:10px}
.ds-report .mdata details .tbl{margin-top:8px}
.ds-report .chip{vertical-align:middle}
/* modules */
.ds-report .idcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.ds-report .idcard{display:flex;align-items:center;justify-content:space-between;gap:12px;border-radius:8px;border:1px solid var(--border);background:var(--card);padding:16px;min-width:0}
.ds-report .idcard>div{min-width:0}
.ds-report .id-label{font:11px/16px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--muted-2)}
.ds-report .id-value{margin-top:4px;font:600 16px/24px var(--mono);letter-spacing:.05em;color:var(--fg);overflow-wrap:anywhere}
.ds-report .id-sub{margin-top:2px;font-size:12px;color:var(--muted);overflow-wrap:anywhere}
/* terminal */
.ds-report .copy{display:inline-flex;align-items:center;gap:6px;padding:2px 4px;border:0;border-radius:4px;background:none;cursor:pointer;font:12px var(--mono);color:var(--muted);transition:color .15s}
.ds-report .copy:hover{color:var(--fg-2)}
.ds-report .copy:focus-visible{outline:2px solid rgb(0 229 255/.6)}
.ds-report .copy .ic{width:12px;height:12px}
.ds-report .term{border-radius:8px;border:1px solid var(--border);background:var(--bg);padding:16px;font:11px/18px var(--mono);color:var(--muted);overflow-x:auto}
.ds-report .term-head{display:flex;align-items:center;gap:8px;padding-bottom:4px;margin-bottom:8px;border-bottom:1px solid #18181b;color:var(--muted-2)}
.ds-report .term-head .dot{background:#34d399}
.ds-report .term p{white-space:pre-wrap}
.ds-report .term .ts{color:var(--faint)}
.ds-report .term .tx{color:#34d399}
.ds-report .term .rx{color:var(--muted-2)}
.ds-report .term .ok{color:rgb(52 211 153/.9);padding-top:4px}
.ds-report .term details{margin:8px 0;padding-top:8px;border-top:1px solid #18181b}
.ds-report .term summary{cursor:pointer;color:var(--muted-2);width:max-content}
.ds-report .term summary:hover{color:var(--fg-3)}
.ds-report .term summary:focus-visible{outline:2px solid rgb(0 229 255/.6)}
.ds-report .term details>div{margin-top:8px;max-height:360px;overflow-y:auto}
.ds-report .foot{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;padding:20px 0 12px;border-top:1px solid rgb(39 39 42/.8);font-size:12px;color:var(--muted-2)}
.ds-report .foot .mono{font-size:11px}
@media (max-width:1100px){.ds-report .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:640px){.ds-report .rows>div{grid-template-columns:1fr;gap:4px}.ds-report .barcol{width:96px}}
@media print{.ds-report .tabs,.ds-report .filters,.ds-report .copy{display:none}.ds-report .pulse{animation:none}}
"""


def e(x) -> str:
    return escape("" if x is None else str(x))


def icon(name: str, cls: str = "ic") -> str:
    return f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def chip(label: str, tone: str = "zinc", plain: bool = False) -> str:
    return f'<span class="chip t-{tone}{" plain" if plain else ""}">{e(label)}</span>'


def _fmt(v, lang: str) -> str:
    if isinstance(v, float):
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return s.replace(".", ",") if lang in DECIMAL_COMMA else s
    return e(text(v, lang))


def _table(headers: list, rows: list[str], extra: str = "") -> str:
    """headers: escaped labels, or (label, css class) pairs."""
    def th(h):
        label, cls = h if isinstance(h, tuple) else (h, "")
        return f'<th class="{cls}">{label}</th>'
    head = f'<thead><tr>{"".join(th(h) for h in headers)}</tr></thead>' if headers else ""
    return (f'<div class="tbl"><div class="tbl-scroll"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'
            f'{extra}</div>')


def _section(sid: str, title: str, body: str, badges: str = "", meta: str = "") -> str:
    return (f'<section id="{sid}"><div class="sec-head"><div class="sec-title"><h2>{e(title)}</h2>{badges}</div>'
            f'{meta}</div>{body}</section>')


def _scale(row: dict) -> tuple[float, str] | None:
    """Position of the value on the bar (0-100) and its tone, from the typical range."""
    rng, v = row.get("normal_range"), row.get("value")
    if not rng or not isinstance(v, (int, float)):
        return None
    lo, hi = rng
    width = (hi - lo) or 1
    vmin = min(lo - width * 0.5, row.get("min", v))
    vmax = max(hi + width * 0.5, row.get("max", v))
    pos = max(2.0, min(100.0, (v - vmin) * 100 / ((vmax - vmin) or 1)))
    if not lo <= v <= hi:
        tone = "red"
    elif min(v - lo, hi - v) < width * 0.1:
        tone = "amber"
    else:
        tone = "green"
    return pos, tone


def render_ai(ai: dict, lang: str) -> str:
    T = partial(t, lang=lang)
    status, drive = ai.get("overall_status", ""), ai.get("safe_to_drive", "")
    chips = []
    if status in STATUS_TONE:
        chips.append(chip(T(f"report.status.{status}"), STATUS_TONE[status], plain=True))
    if drive in DRIVE_TONE:
        chips.append(chip(T(f"drive.{drive}"), DRIVE_TONE[drive], plain=True))
    parts = [f'<div class="card"><div class="chips">{"".join(chips)}</div>',
             f"<p>{e(ai.get('overall_assessment'))}</p>",
             f'<p class="muted">{e(ai.get("safe_to_drive_reason"))}</p>']
    ai_lang = ai.get("_language")
    if ai_lang and normalize(ai_lang) != normalize(lang):
        parts.append(f'<p class="note">{e(T("report.ai_other_language", language=T(f"lang.{normalize(ai_lang)}")))}</p>')
    parts.append("</div>")

    findings = ai.get("findings", [])
    if findings:
        parts.append(f'<h3 class="caps">{e(T("report.ai_fix_heading"))}</h3><div class="stack">')
        for f in findings:
            sev = f.get("severity", "info")
            tone = SEV_TONE.get(sev, "zinc")
            head = [chip(T(f"severity.{sev}"), tone), f'<span class="sys">{e(f.get("system"))}</span>',
                    chip(T(f"urgency.{f.get('urgency')}"), "zinc", plain=True),
                    chip(T(f"diy.{f.get('diy_difficulty')}"), "zinc", plain=True)]
            head += [f'<span class="code mono t-{tone}">{e(c)}</span>' for c in f.get("related_codes", [])]
            causes = "".join(
                f"<li>{e(c.get('cause'))} <span class='muted'>· {e(T('report.likelihood', level=T('likelihood.' + str(c.get('likelihood')))))}</span></li>"
                for c in f.get("likely_causes", []))
            tests = "".join(f"<li>{e(x)}</li>" for x in f.get("tests", []))
            fixes = "".join(f"<li>{e(x)}</li>" for x in f.get("fixes", []))
            parts.append(f"""<article class="fcard t-{tone}">
<div class="fcard-head">{"".join(head)}</div>
<h3>{e(f.get('title'))}</h3><p>{e(f.get('explanation'))}</p>
<p class="evidence"><b>{e(T("report.evidence"))}</b> {e(f.get('evidence'))}</p>
<dl class="rows">
<div><dt>{e(T("report.likely_causes"))}</dt><dd><ul>{causes}</ul></dd></div>
<div><dt>{e(T("report.what_to_test"))}</dt><dd><ol>{tests}</ol></dd></div>
<div><dt>{e(T("report.how_to_fix"))}</dt><dd><ul>{fixes}</ul></dd></div>
</dl></article>""")
        parts.append("</div>")

    watch = ai.get("watch_list", [])
    if watch:
        parts.append(f'<h3 class="caps">{e(T("report.watch_heading"))}</h3>' + _table(
            [e(T("report.col_item")), e(T("report.col_why")), e(T("report.col_how"))],
            [f"<tr><td class='strong'>{e(w.get('item'))}</td><td>{e(w.get('why'))}</td><td>{e(w.get('how_to_monitor'))}</td></tr>"
             for w in watch]))
    for key, heading in (("data_gaps", "report.gaps_heading"), ("maintenance_tips", "report.tips_heading")):
        items = ai.get(key, [])
        if items:
            parts.append(f'<h3 class="caps">{e(T(heading))}</h3><div class="card card-list"><ul>'
                         + "".join(f"<li>{e(i)}</li>" for i in items) + "</ul></div>")
    meta = f'<span class="sec-meta">{e(ai["_model"])}</span>' if ai.get("_model") else ""
    body = "".join(parts)
    if ai.get("_model"):
        body += f'<p class="note">{e(T("report.ai_by", model=ai["_model"]))}</p>'
    return _section("r-ai", T("report.ai_heading"), body, meta=meta)


def render_body(report: dict, ai: dict | None = None, lang: str = "en") -> str:
    lang = normalize(lang)
    T = partial(t, lang=lang)

    def X(value) -> str:  # escaped text of any report value
        return e(text(value, lang))

    def definition_label(d: dict) -> str:
        """Which Renault definition a module was read with, and how sure the match is."""
        if not d:
            return ""
        if d.get("status") == "rejected":
            return f'{chip(T("report.def_rejected"), "red")} {X(d.get("reason", ""))}'
        how = T("report.def_chosen") if d.get("chosen") == "user" else T(f"report.tier_{d.get('tier')}")
        return (f'{e(T("report.def_used"))}: <span class="mono">{e(d.get("ecuname", ""))}</span> '
                f'{chip(how, TIER_TONE.get(d.get("tier"), "zinc"))}')

    meta, veh, ad = report.get("meta", {}), report.get("vehicle", {}), report.get("adapter", {})
    summ, obd = report.get("summary", {}), report.get("obd", {})
    ecus = report.get("ecus", [])
    findings = report.get("findings", [])
    status = summ.get("status", "good")
    counts = summ.get("severity_counts", {})
    total_codes = summ.get("total_fault_codes", 0)
    mon = obd.get("monitor_status") or {}
    supported_mon = [m for m in mon.get("monitors", []) if m["supported"]]
    incomplete = [m for m in supported_mon if not m["complete"]]
    out = [f'<div class="ds-report" lang="{lang}"><style>{CSS}</style>']

    # -- hero ------------------------------------------------------------------
    scanned = str(meta.get("scanned_at", ""))
    vin = veh.get("vin")
    stone = STATUS_TONE.get(status, "zinc")
    sub = [f'<strong>{e(meta.get("vehicle_hint", ""))}</strong>',
           f'<span class="sep">•</span><span class="ts">{icon("clock")}{e(T("report.scanned_at", date=scanned))}</span>']
    if vin:
        sub.append(f'<span class="sep">•</span><span class="vin">{e(T("report.vin"))}: <code>{e(vin)}</code></span>')
    if meta.get("odometer_km"):
        src = meta.get("odometer_source")
        km = T("report.odometer", km=number(int(meta["odometer_km"]), lang))
        if src and src != "user":
            km += f' ({T("report.odometer_from", module=T(f"ecu.{src}"))})'
        sub.append(f'<span class="sep">•</span><span class="ts">{e(km)}</span>')
    status_box = (f'<div class="status-box t-{stone}">{icon(STATUS_ICON.get(status, "alert-triangle"))}<div>'
                  f'<div class="sb-label">{e(T(f"report.status_label.{status}"))}</div>'
                  f'<div class="sb-text">{e(T(f"report.status.{status}"))}</div></div></div>')

    v_start, v_end = ad.get("battery_voltage_start"), ad.get("battery_voltage_end")
    battery = f"{_fmt(v_start, lang)} V" + (f" → {_fmt(v_end, lang)} V" if v_end else "") if v_start else "–"
    btone = "zinc" if v_start is None else "red" if v_start < 12.0 else "amber" if v_start < 12.4 or v_start > 15 else "green"
    battery_sub = ""
    bt = report.get("battery_test")
    if bt:  # the guided test says more than two readings
        grades = [bt.get(k) for k in ("rest", "crank", "idle", "load") if bt.get(k)]
        btone = max((GRADE_TONE.get(g, "zinc") for g in grades), key=TONE_RANK.index, default="zinc")
        battery = f'{_fmt(bt.get("rest_v"), lang)} V' if bt.get("rest_v") is not None else "–"
        if bt.get("crank_min_v") is not None:
            battery_sub = T("report.battery_test_kpi", crank=_fmt(bt["crank_min_v"], lang))
    protocol = str(ad.get("protocol", ""))
    p_main, _, p_detail = protocol.partition(" (")
    mil_on = summ.get("mil_on")
    kpis = [
        (f't-{"red" if total_codes else "green"}', T("report.fault_codes"),
         f'<div class="kpi-big"><b>{e(total_codes)}</b>{chip("DTC", "red" if total_codes else "green")}</div>'),
        ("", T("report.modules_answering"),
         f'<div class="kpi-big"><b>{e(summ.get("modules_responding", 0))}</b><span>{e(T("report.of_total", total=summ.get("modules_checked", 0)))}</span></div>'),
        (f't-{"red" if mil_on else "green"}', T("report.mil"),
         f'<div class="kpi-val toned"><span class="dot{" pulse" if mil_on else ""}"></span>{e((T("report.on") if mil_on else T("report.off")).upper())}</div>'),
        (f"t-{btone}" if btone != "green" else "", T("report.battery"),
         f'<div><div class="kpi-val toned t-{btone}">{icon("activity")}<span>{battery}</span></div>'
         + (f'<span class="kpi-sub">{e(battery_sub)}</span>' if battery_sub else "") + "</div>"),
        ("", T("report.protocol"),
         f'<div><span class="kpi-val" title="{e(protocol)}">{e(p_main)}</span><span class="kpi-sub">{e(p_detail.rstrip(")"))}</span></div>'),
        ("", T("report.adapter"),
         f'<div><span class="kpi-val">{e(ad.get("version", ""))}</span><span class="kpi-sub">{e(ad.get("port", ""))}</span></div>'),
        ("", T("report.scan"),
         f'<div class="kpi-row"><span class="kpi-val">{e(T(f"scan_type.{meta.get("scan_type", "standard")}"))}</span>'
         f'<span class="kpi-sub">{e(meta.get("duration_s", ""))} s</span></div>'),
        (f't-{"amber" if incomplete else "green"}' if supported_mon else "", T("report.kpi_readiness"),
         (f'<div class="kpi-row"><span class="kpi-val">{e(T("report.of_complete", done=len(supported_mon) - len(incomplete), total=len(supported_mon)))}</span>'
          f'{chip(f"{len(supported_mon) - len(incomplete)}/{len(supported_mon)}", "amber" if incomplete else "green")}</div>')
         if supported_mon else '<span class="kpi-val">–</span>'),
    ]
    kpi_html = "".join(f'<div class="kpi {cls}"><span class="kpi-label">{e(label)}</span>{body}</div>' for cls, label, body in kpis)
    out.append(f"""<section class="hero" id="r-top"><div class="hero-top"><div><h1>{e(T("report.title"))}</h1>
<div class="subline">{"".join(sub)}</div></div>{status_box}</div><div class="kpis">{kpi_html}</div></section>""")

    # -- tabs --------------------------------------------------------------------
    tabs = []
    if ai:
        tabs.append(("r-ai", T("report.tab_ai"), ""))
    with_data = [m for m in ecus if m.get("module_data")]
    tabs += [
        ("r-checks", T("report.automatic_checks"), f'<span class="count">{len(findings)}</span>'),
        ("r-codes", T("report.codes_heading"), f'<span class="count{" t-red" if total_codes else ""}">{total_codes}</span>'),
        ("r-emissions", T("report.tab_emissions"), ""),
        ("r-live", T("report.tab_live"), ""),
        *([("r-battery", T("report.tab_battery"), "")] if bt else []),
        *([("r-moddata", T("report.tab_module_data"), f'<span class="count">{len(with_data)}</span>')] if with_data else []),
        ("r-modules", T("report.tab_modules"), ""),
        ("r-log", T("report.tab_log"), ""),
    ]
    out.append('<nav class="tabs">' + "".join(
        f'<a class="tab{" active" if i == 0 else ""}" href="#{sid}"><span>{e(label)}</span>{extra}</a>'
        for i, (sid, label, extra) in enumerate(tabs)) + "</nav>")

    if ai:
        out.append(render_ai(ai, lang))

    # -- automatic checks ----------------------------------------------------------
    filters = ['<div class="filters"><input type="radio" name="ds-sev" id="sf-all" checked>'
               f'<label class="fbtn all" for="sf-all">{e(T("report.filter_all"))}</label>']
    for s in SEVERITIES:
        if counts.get(s):
            filters.append(f'<input type="radio" name="ds-sev" id="sf-{s}"><label class="fbtn t-{SEV_TONE[s]}" for="sf-{s}">'
                           f'<span class="dot"></span>{e(counts[s])} {e(T(f"severity.{s}"))}</label>')
    filters.append("</div>")
    cards = []
    for f in findings:
        sev = f["severity"]
        tone = SEV_TONE[sev]
        title = text(f["title"], lang)
        code = next((c for c in f.get("codes", []) if title.startswith(c)), None)
        title_html = (f'<span class="code">{e(code)}</span>{e(title[len(code):])}' if code else e(title))
        detail = f'<p>{X(f["detail"])}</p>' if f.get("detail") else ""
        cards.append(f'<article class="fcard sev-{sev} t-{tone}"><div class="fcard-head">{chip(T(f"severity.{sev}"), tone)}'
                     f'<span class="sys">{X(f["system"])}</span></div><h3>{title_html}</h3>{detail}</article>')
    body = f'<div class="stack">{"".join(cards)}</div>' if cards else f'<div class="empty">{e(T("report.no_issues"))}</div>'
    out.append(_section("r-checks", T("report.automatic_checks"), body,
                        badges=f'<span class="count">{e(T("report.records", n=len(findings)))}</span>',
                        meta="".join(filters) if findings else ""))

    # -- fault codes -----------------------------------------------------------------
    rows = []
    direct = engine_direct_states(report)
    direct_entries = engine_direct(report)
    odometer = meta.get("odometer_km")

    def meaning(code: str, d: dict | None) -> str:
        """Renault's name first when the module's definition gave one, the generic meaning below."""
        generic = X(describe(code)[0])
        name = (d or {}).get("renault_name")
        return f'<b>{e(name)}</b><span class="sub">{generic}</span>' if name else generic

    def history(d: dict | None) -> str:
        ext = (d or {}).get("extended") or {}
        parts = []
        if ext.get("mileage_km") is not None:
            km = int(ext["mileage_km"])
            ago = odometer - km if odometer and odometer >= km else None
            parts.append(T("report.stored_at_ago", km=number(km, lang), ago=number(ago, lang)) if ago is not None
                         else T("report.stored_at", km=number(km, lang)))
        if ext.get("occurrences"):
            parts.append(T("report.times", n=ext["occurrences"]))
        return f'<span class="sub">{e(" · ".join(parts))}</span>' if parts else ""

    obd_codes = set()
    for kind, items in obd.get("dtcs", {}).items():
        for d in items:
            obd_codes.add(d["code"])
            tone = OBD_KIND_TONE.get(kind, "zinc")
            state = f'<b>{e(T(f"dtc_kind.{kind}"))}</b>'
            if d["code"] in direct:
                state += f' · {e(T("report.direct_read_only", state=direct[d["code"]]))}'
            de = direct_entries.get(d["code"])
            rows.append((T("report.engine_obd"), d["code"], meaning(d["code"], de), state + history(de), "", tone))
    for ecu in ecus:
        for d in ecu.get("dtcs", []):
            if ecu["tx"] == ENGINE_TX and d["code"] in obd_codes:
                continue
            tone = ECU_STATE_TONE.get(d.get("state_id"), "zinc")
            rows.append((ecu["name"], d["code"], meaning(d["code"], d), f"<b>{X(d['state'])}</b>{history(d)}",
                         d.get("failure_type", ""), tone))
    if rows:
        body = _table([e(T(k)) for k in ("report.col_module", "report.col_code", "report.col_meaning",
                                          "report.col_state", "report.col_failure")],
                      [f'<tr class="t-{r[5]}"><td class="strong">{X(r[0])}</td><td>{chip(r[1], r[5], plain=True)}</td>'
                       f'<td class="meaning">{r[2]}</td><td class="state">{r[3]}</td>'
                       f'<td class="state">{X(r[4]) or "—"}</td></tr>' for r in rows])
    else:
        body = f'<div class="empty">{e(T("report.no_codes"))}</div>'
    out.append(_section("r-codes", T("report.codes_heading"), body,
                        badges=f'<span class="count{" t-red" if total_codes else ""}">{e(T("report.codes_count", n=total_codes))}</span>',
                        meta='<span class="sec-meta">SAE J2012 / ISO 15031-6</span>'))

    # -- emissions: readiness + freeze frame ------------------------------------------
    if supported_mon:
        cells = "".join(
            f'<div class="t-{"green" if m["complete"] else "amber"}"><span>{X(m["name"])}</span>'
            f'{chip(T("report.complete") if m["complete"] else T("report.not_complete"), "green" if m["complete"] else "amber", plain=True)}</div>'
            for m in supported_mon)
        body = f'<div class="ready">{cells}</div><p class="note">{e(T("report.ignition", type=mon["ignition"]))}</p>'
    else:
        body = f'<div class="empty">{e(T("report.no_readiness"))}</div>'
    ff = obd.get("freeze_frame")
    if ff:
        tiles = []
        for v in ff["values"]:
            row = {"value": v["value"], "normal_range": None}
            pid = PIDS.get(int(v["pid"], 16))
            if pid and pid.normal and isinstance(v["value"], (int, float)):
                row["normal_range"] = list(pid.normal)
            sc = _scale(row)
            tone = "red" if sc and sc[1] == "red" else ("green" if isinstance(v["value"], dict) and v["value"].get("key") in ("fuel_status.2",) else "")
            tiles.append(f'<div class="tile{f" t-{tone}" if tone else ""}"><span class="tl" title="{X(v["name"])}">{X(v["name"])}</span>'
                         f'<span class="tv">{_fmt(v["value"], lang)} {e(v["unit"])}</span></div>')
        desc = T("report.freeze_intro", code=ff["trigger_dtc"], desc=describe(ff["trigger_dtc"])[0]).rstrip(":")
        body += (f'<div class="card"><div class="ff-head"><div><h3 class="caps"><span>{e(T("report.freeze_heading"))}</span>'
                 f'{chip(ff["trigger_dtc"], "red", plain=True)}</h3><p class="ff-desc">{e(desc)}.</p></div>'
                 f'<span class="sec-meta">{e(T("report.mode02"))}</span></div><div class="tiles">{"".join(tiles)}</div></div>')
    em_meta = (f'<span class="sec-meta t-amber"><span class="dot"></span>{e(T("report.incomplete_count", n=len(incomplete)))}</span>'
               if incomplete else "")
    out.append(_section("r-emissions", T("report.readiness_heading"), body, meta=em_meta))

    # -- live data ----------------------------------------------------------------------
    live = obd.get("live_data", [])
    table_rows, meta_pairs = [], []
    for row in live:
        if not row.get("normal_range") and "samples" not in row:
            meta_pairs.append(f'<span>{X(row["name"])}: <b>{_fmt(row.get("value"), lang)}{(" " + e(row["unit"])) if row["unit"] else ""}</b></span>')
            continue
        v, rng = row.get("value"), row.get("normal_range")
        sc = _scale(row)
        tone = sc[1] if sc else ""
        flag = ""
        if tone == "red":
            flag = chip(T("report.value_high") if v > rng[1] else T("report.value_low"), "red", plain=True)
        rng_txt = f"{_fmt(rng[0], lang)} – {_fmt(rng[1], lang)} {e(row['unit'])}" if rng else "—"
        if "min" in row and row["min"] != row["max"]:
            rng_txt += f' · {e(T("report.seen", min=_fmt(row["min"], lang), max=_fmt(row["max"], lang)))}'
        note = f'<span class="sub">{X(row["note"])}</span>' if row.get("note") else ""
        bar = f'<div class="fillbar t-{tone}"><i style="width:{sc[0]:.0f}%"></i></div>' if sc else ""
        val_tone = f" t-{tone}" if tone in ("red", "amber") else ""
        table_rows.append(
            f'<tr class="{"hot" if tone == "red" else ""}"><td class="strong">{X(row["name"])}{note}</td>'
            f'<td class="t-val{val_tone}">{_fmt(v, lang)} {e(row["unit"])}{flag}</td>'
            f'<td class="range">{rng_txt}</td><td class="barcol">{bar}</td></tr>')
    if table_rows or meta_pairs:
        metabar = (f'<div class="metabar"><div>{"".join(meta_pairs)}</div>'
                   f'<div>{e(T("report.typical_note"))}</div></div>')
        body = _table([e(T("report.col_value")), e(T("report.col_reading")), e(T("report.col_typical")), ""],
                      table_rows, metabar) if table_rows else f'<div class="tbl">{metabar}</div>'
    else:
        body = f'<div class="empty">{e(T("report.no_live"))}</div>'
    out.append(_section("r-live", T("report.live_heading"), body,
                        badges=f'<span class="count">{e(T("report.params_count", n=len(live)))}</span>' if live else "",
                        meta=f'<span class="sec-meta">{e(T("report.mode01"))}</span>'))

    # -- battery & charging test ------------------------------------------------------------
    if bt:
        cells = []
        for key, v, grade, sub_text in (
                ("rest", bt.get("rest_v"), bt.get("rest"), ""),
                ("crank", bt.get("crank_min_v"), bt.get("crank"),
                 T("report.batt_brownout") if bt.get("brownout") else
                 T("report.batt_seconds", s=_fmt(bt["crank_s"], lang)) if bt.get("crank_s") else ""),
                ("idle", bt.get("idle_v"), bt.get("idle"),
                 T("report.batt_ecu", v=_fmt(bt["ecu_v"], lang)) if bt.get("ecu_v") else ""),
                ("load", bt.get("load_v"), bt.get("load"), "")):
            tone = GRADE_TONE.get(grade, "zinc")
            cells.append(f'<div class="tile t-{tone}"><span class="tl">{e(T(f"report.batt_{key}"))}</span>'
                         f'<span class="tv">{_fmt(v, lang) + " V" if v is not None else "–"}</span>'
                         f'{chip(T(f"report.grade_{grade}") if grade else "–", tone)}'
                         f'<span class="tsub">{e(sub_text)}</span></div>')
        body = (f'<div class="tiles">{"".join(cells)}</div>'
                f'<p class="note">{e(T("report.batt_note", at=str(bt.get("at", ""))[:16].replace("T", " ")))}</p>')
        out.append(_section("r-battery", T("report.battery_heading"), body))

    # -- module data (Renault database) --------------------------------------------------------
    if with_data:
        blocks = []
        for m in with_data:
            data = m["module_data"]
            groups = []
            for i, g in enumerate(data.get("groups", [])):
                trs = "".join(
                    f'<tr><td>{e(v["item"])}</td><td class="mono nowrap">{_fmt(v["value"], lang)}'
                    f'{(" " + e(v["unit"])) if v.get("unit") else ""}</td></tr>' for v in g["values"])
                groups.append(f'<details{" open" if i < 2 else ""}><summary>{X(g["name"])}'
                              f'<span class="count">{len(g["values"])}</span></summary>'
                              f'<div class="tbl"><table><tbody>{trs}</tbody></table></div></details>')
            cut = f'<p class="note">{e(T("report.module_partial", n=data.get("read", 0), total=data.get("total", 0)))}</p>' \
                if data.get("partial") else ""
            blocks.append(f'<div class="mdata"><div class="mdata-head"><b>{X(m["name"])}</b>'
                          f'<span class="sub">{definition_label(m.get("definition") or {})}</span></div>'
                          f'{"".join(groups)}{cut}</div>')
        out.append(_section("r-moddata", T("report.module_data_heading"), "".join(blocks),
                            meta=f'<span class="sec-meta">{e(T("report.module_data_meta"))}</span>'))

    # -- modules & hardware ---------------------------------------------------------------
    parts = []
    cal = veh.get("calibration_ids", [])
    extra = [*veh.get("ecu_names", []), *veh.get("calibration_verification_numbers", [])]
    if veh:
        parts.append('<div class="idcards">')
        parts.append(f'<div class="idcard"><div><div class="id-label">{e(T("report.vin_card"))}</div>'
                     f'<p class="id-value">{e(vin or T("report.not_reported"))}</p>'
                     f'<p class="id-sub">{e(meta.get("vehicle_hint", ""))}</p></div></div>')
        if cal or extra:
            parts.append(f'<div class="idcard"><div><div class="id-label">{e(T("report.calibration_card"))}</div>'
                         f'<p class="id-value">{e(", ".join(cal) or "—")}</p>'
                         f'<p class="id-sub">{e(" · ".join(extra))}</p></div></div>')
        parts.append("</div>")
    if ecus:
        trs = []
        for m in ecus:
            on = m["responded"]
            ident = "".join(
                f'<span class="sub">{e(T(f"ident.{k}") if has(f"ident.{k}") else k)}: <span class="mono">{e(v)}</span></span>'
                for k, v in m["identification"].items() if k != "raw")
            notes = "".join(f'<span class="sub">{X(n)}</span>' for n in m.get("notes", []))
            if m.get("definition"):
                notes = f'<span class="sub">{definition_label(m["definition"])}</span>' + notes
            dtcs = m.get("dtcs", [])
            via_obd = m.get("dtc_via_obd")
            if via_obd:
                dtcs = [d for items in report.get("obd", {}).get("dtcs", {}).values()
                        for d in items if d.get("ecu") == m["rx"]]
            if not on:
                codes = "—"
            elif not m.get("dtc_read_ok") and not via_obd:
                codes = "?"
            else:
                ctone = "red" if any(d.get("state_id") == "active" for d in dtcs) else "yellow" if dtcs else "green"
                codes = f'<span class="toned t-{ctone}">{e(T("report.codes_count", n=len(dtcs)))}</span>'
                if via_obd:
                    codes += f'<span class="sub">{e(T("report.via_obd"))}</span>'
            status_html = (f'<span class="online t-green"><span class="dot"></span>{e(T("report.online"))}</span>' if on
                           else f'<span class="online t-zinc"><span class="dot"></span>{e(T("report.no_answer"))}</span>')
            proto = f'<span class="mono toned t-green">{e(m["protocol"])}</span>' if on else "—"
            trs.append(f'<tr class="{"" if on else "dim"}"><td class="strong">{X(m["name"])}</td>'
                       f'<td class="mono nowrap">{e(m["tx"])} / {e(m["rx"])}</td><td>{proto}</td>'
                       f'<td>{ident}{notes}</td><td>{status_html}</td><td class="mono nowrap">{codes}</td></tr>')
        parts.append(_table([e(T("report.col_module")), e(T("report.col_can")), e(T("report.protocol")),
                             e(T("report.col_ident")), e(T("report.col_status")), "DTC"], trs))
    else:
        parts.append(f'<div class="empty">{e(T("report.no_modules"))}</div>')
    out.append(_section("r-modules", T("report.modules_heading"), "".join(parts),
                        badges=f'<span class="count">{e(T("report.modules_active", n=summ.get("modules_responding", 0), total=summ.get("modules_checked", 0)))}</span>' if ecus else "",
                        meta='<span class="sec-meta">ISO 11898</span>'))

    # -- technical log ----------------------------------------------------------------------
    log_lines = []
    for line in report.get("scan_log", []):
        if line.startswith("[") and "] " in line:
            ts, msg_ = line[1:].split("] ", 1)
            log_lines.append(f'<p><span class="ts">[{e(ts)}]</span> {e(msg_)}</p>')
        else:
            log_lines.append(f"<p>{e(line)}</p>")
    raw = report.get("adapter_log", [])
    raw_html = ""
    if raw:
        raw_lines = "".join(
            f'<p><span class="ts">[+{float(r[0]):.3f}s]</span> <span class="{"tx" if r[1] == "TX" else "rx"}">'
            f'{"TX &gt;" if r[1] == "TX" else "RX &lt;"}</span> {e(r[2])}</p>' for r in raw)
        raw_html = f'<details><summary>{e(T("report.raw_traffic", n=len(raw)))}</summary><div>{raw_lines}</div></details>'
    session = T("report.session", id=scanned.replace("-", "").replace(":", "")[:15], protocol=protocol)
    term = (f'<div class="term" id="ds-log"><div class="term-head"><span class="dot"></span><span>{e(session)}</span></div>'
            f'{"".join(log_lines)}{raw_html}'
            f'<p class="ok">{e(T("report.log_done", seconds=meta.get("duration_s", "")))}</p></div>')
    copy_btn = (f'<button class="copy" data-copy="#ds-log">{icon("copy")}'
                f'<span class="copy-label">{e(T("report.copy_log"))}</span></button>')
    out.append(_section("r-log", T("report.scan_log"), term, badges='<span class="count">ELM327</span>', meta=copy_btn))

    out.append(f'<footer class="foot"><span class="mono">DusterScan v{e(meta.get("app_version", ""))}</span>'
               f'<span>{e(T("report.footer"))}</span></footer></div>')
    return "".join(out)


_STANDALONE_JS = """document.addEventListener('click',function(e){var c=e.target.closest('[data-copy]');
if(c){var t=document.querySelector(c.dataset.copy);if(t&&navigator.clipboard)navigator.clipboard.writeText(t.innerText);return}
var b=e.target.closest('.tab');if(b)b.parentElement.querySelectorAll('.tab').forEach(function(x){x.classList.toggle('active',x===b)})});"""


def render_report(report: dict, ai: dict | None = None, lang: str = "en") -> str:
    """A complete standalone HTML document (for saving / printing)."""
    lang = normalize(lang)
    vin = report.get("vehicle", {}).get("vin") or ""
    return (f"<!doctype html><html lang='{lang}'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(t('report.doc_title', lang, vin=vin))}</title>"
            "<style>html{color-scheme:dark;scroll-behavior:smooth}body{margin:0;background:#09090b}"
            ".page{max-width:1200px;margin:0 auto;padding:24px 16px}</style>"
            f"</head><body><div class='page'>{render_body(report, ai, lang)}</div>"
            f"<script>{_STANDALONE_JS}</script></body></html>")
