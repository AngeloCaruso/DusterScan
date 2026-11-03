"""Rule-based first pass over the scan data.

These checks need no internet and give a useful report on their own. The AI
analysis then goes deeper, using both the raw data and these findings.
Titles and details are stored as translatable messages (see i18n.msg).
"""

from __future__ import annotations

from .i18n import msg
from .obd2 import CLOSED_LOOP

SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
ENGINE_TX = "7E0"
SAFETY_KINDS = {"abs", "steering", "airbag"}


def engine_direct(report: dict) -> dict[str, dict]:
    """Code -> the entry the engine computer gave when addressed directly (state, Renault details)."""
    for ecu in report.get("ecus", []):
        if ecu.get("tx") == ENGINE_TX:
            return {d["code"]: d for d in ecu.get("dtcs", [])}
    return {}


def engine_direct_states(report: dict) -> dict[str, dict]:
    """Code -> state message as reported by addressing the engine computer directly."""
    return {code: d["state"] for code, d in engine_direct(report).items()}


def _live(report: dict) -> dict[str, dict]:
    return {row["pid"]: row for row in report.get("obd", {}).get("live_data", [])}


def _val(live: dict, pid: str, key: str = "avg"):
    row = live.get(pid)
    if not row:
        return None
    v = row.get(key, row.get("value"))
    return v if isinstance(v, (int, float)) else None


def _finding(severity: str, system, name: str | None = None, codes=None,
             title=None, detail=None, **params) -> dict:
    """A finding whose texts are "finding.<name>.title" / ".detail" unless given explicitly."""
    if system is not None and isinstance(system, str):
        system = msg(f"system.{system}")
    return {
        "severity": severity,
        "system": system,
        "title": title or msg(f"finding.{name}.title", **params),
        "detail": detail if detail is not None else (msg(f"finding.{name}.detail", **params) if name else ""),
        "codes": codes or [],
    }


def _obd_code_severity(code: str) -> str:
    if code.startswith("P030") or code.startswith("P035"):
        return "high"  # misfire / ignition: can damage the catalytic converter
    if code in ("P0217", "P0118", "P0116", "P0520", "P0562", "P0563"):
        return "high"
    return "medium"


def _ecu_code_severity(kind: str, d: dict) -> str:
    safety = kind in SAFETY_KINDS
    if d["state_id"] == "active" or d.get("status", {}).get("warning_lamp"):
        return "high" if safety else "medium"
    if d["state_id"] == "stored":
        return "medium" if safety else "low"
    return "low"


# Code age, from the mileage the module stored with the code (Renault database)
OLD_KM = 5000      # a single old occurrence matters less
RECENT_KM = 500    # a recent code in a safety system matters more


def _code_age(d: dict | None, odometer: int | None) -> tuple[int | None, int | None]:
    """(km since the code was stored, occurrences); None when unknown."""
    ext = (d or {}).get("extended") or {}
    km = ext.get("mileage_km")
    ago = odometer - km if odometer and km is not None and odometer >= km else None
    return ago, ext.get("occurrences")


def _age_adjusted(sev: str, d: dict | None, odometer: int | None, safety: bool) -> str:
    ago, times = _code_age(d, odometer)
    if ago is None or (d or {}).get("state_id") == "active":
        return sev
    i = SEVERITY_ORDER.index(sev)
    if ago > OLD_KM and (times or 1) <= 1 and i < SEVERITY_ORDER.index("low"):
        return SEVERITY_ORDER[i + 1]
    if ago < RECENT_KM and safety and i > SEVERITY_ORDER.index("medium"):
        return "medium"
    return sev


def _history(d: dict | None, odometer: int | None):
    """" Stored at 89,420 km (580 km ago), 3 times." when the module recorded it."""
    ext = (d or {}).get("extended") or {}
    parts = []
    km = ext.get("mileage_km")
    ago, times = _code_age(d, odometer)
    if km is not None:
        parts.append(msg("finding.history_km_ago", km=km, ago=ago) if ago is not None
                     else msg("finding.history_km", km=km))
    if times:
        parts.append(msg("finding.history_times", n=times))
    return msg("finding.history", parts=parts) if parts else ""


def _renault(d: dict | None):
    name = (d or {}).get("renault_name")
    return msg("finding.renault_name", name=name) if name else ""


def _is_network_code(d: dict) -> bool:
    """U codes (communication between modules, supply voltage) that are not failing right now.

    Several modules storing these together is the footprint of one event -
    a voltage dip, a jump start, a battery disconnected with the ignition on -
    so they are reported as one finding instead of one per code.
    """
    return d["code"].startswith("U") and d["state_id"] != "active"


SUPPLY_DROP_V = 0.5   # a module reading this much below the OBD socket voltage


def _battery_test_findings(bt: dict) -> list[dict]:
    out = []
    if bt.get("crank") == "weak":
        name = "batt_test_brownout" if bt.get("brownout") else "batt_test_crank_weak"
        out.append(_finding("medium", "battery", name, v=bt.get("crank_min_v") or 0))
    elif bt.get("crank") == "fair":
        out.append(_finding("low", "battery", "batt_test_crank_fair", v=bt["crank_min_v"]))
    if bt.get("rest") == "low":
        out.append(_finding("medium", "battery", "batt_test_rest_low", v=bt["rest_v"]))
    elif bt.get("rest") == "fair":
        out.append(_finding("low", "battery", "batt_test_rest_fair", v=bt["rest_v"]))
    if bt.get("idle") == "low":
        out.append(_finding("high", "charging", "batt_test_charge_low", v=bt["idle_v"]))
    elif bt.get("idle") == "high":
        out.append(_finding("medium", "charging", "batt_test_charge_high", v=bt["idle_v"]))
    if bt.get("load") == "low" and bt.get("idle") != "low":
        out.append(_finding("medium", "charging", "batt_test_load_low", v=bt["load_v"], idle=bt.get("idle_v") or 0))
    if not out and bt.get("crank"):
        out.append(_finding("info", "battery", "batt_test_ok", rest=bt.get("rest_v") or 0,
                            crank=bt.get("crank_min_v") or 0, idle=bt.get("idle_v") or 0))
    return out


def evaluate(report: dict) -> list[dict]:
    f: list[dict] = []
    obd = report.get("obd", {})
    live = _live(report)
    mon = obd.get("monitor_status") or {}
    rpm = _val(live, "0C")
    running = rpm is not None and rpm > 400
    speed = _val(live, "0D", "value") or 0
    coolant = _val(live, "05", "max")
    runtime = _val(live, "1F", "value")

    # Engine state
    if not running:
        f.append(_finding("info", "scan", "engine_off"))

    # Check-engine light
    if mon.get("mil_on"):
        f.append(_finding("high", "engine", "mil", count=mon.get("dtc_count", "?")))

    # OBD-II DTCs (merged with the engine computer's own direct read, which
    # usually reports the same codes)
    dtcs = obd.get("dtcs", {})
    stored_codes = {d["code"] for d in dtcs.get("stored", [])}
    obd_codes = {d["code"] for items in dtcs.values() for d in items}
    direct = engine_direct(report)
    odometer = report.get("meta", {}).get("odometer_km")

    def also(code):
        d = direct.get(code)
        if not d:
            return ""
        return msg("finding.direct_more", direct=msg("finding.direct_state", state=d["state"]),
                   renault=_renault(d), history=_history(d, odometer))

    for d in dtcs.get("stored", []):
        sev = _age_adjusted(_obd_code_severity(d["code"]), direct.get(d["code"]), odometer, False)
        f.append(_finding(sev, "engine", codes=[d["code"]],
                          title=msg("finding.code_title", code=d["code"], desc=d["description"]),
                          detail=msg("finding.obd_stored.detail", also=also(d["code"]))))
    for d in dtcs.get("pending", []):
        if d["code"] in stored_codes:
            continue
        sev = "medium" if _obd_code_severity(d["code"]) == "high" else "low"
        f.append(_finding(sev, "engine", codes=[d["code"]],
                          title=msg("finding.code_pending_title", code=d["code"], desc=d["description"]),
                          detail=msg("finding.obd_pending.detail", also=also(d["code"]))))
    for d in dtcs.get("permanent", []):
        if d["code"] in stored_codes:
            continue
        f.append(_finding("info", "engine", codes=[d["code"]],
                          title=msg("finding.code_permanent_title", code=d["code"], desc=d["description"]),
                          detail=msg("finding.obd_permanent.detail")))

    # Individual ECUs
    silent = []
    network: dict[str, list] = {}   # code -> modules, for the grouped finding
    network_modules = []
    network_times: dict[str, int] = {}   # code -> most occurrences any module counted
    network_ago: dict[str, int] = {}     # code -> most recent km ago
    for ecu in report.get("ecus", []):
        if not ecu.get("responded"):
            silent.append(ecu["name"])
            continue
        if not ecu.get("dtc_read_ok") and not ecu.get("dtc_via_obd"):
            notes = ecu.get("notes") or [msg("finding.dtc_unreadable.detail")]
            f.append(_finding("info", ecu["name"], title=msg("finding.dtc_unreadable.title", ecu=ecu["name"]),
                              detail=notes))
        for d in ecu.get("dtcs", []):
            if ecu["tx"] == ENGINE_TX and d["code"] in obd_codes:
                continue  # already reported from the OBD-II read above
            if _is_network_code(d):
                network.setdefault(d["code"], [])
                if ecu["name"] not in network[d["code"]]:
                    network[d["code"]].append(ecu["name"])
                if ecu["name"] not in network_modules:
                    network_modules.append(ecu["name"])
                ago, times = _code_age(d, odometer)
                if times:
                    network_times[d["code"]] = max(network_times.get(d["code"], 0), times)
                if ago is not None:
                    network_ago[d["code"]] = min(network_ago.get(d["code"], ago), ago)
                continue
            ftb = msg("finding.ftb_suffix", ftb=d["failure_type"]) if d.get("failure_type") else ""
            safety = ecu.get("kind", "") in SAFETY_KINDS
            sev = _age_adjusted(_ecu_code_severity(ecu.get("kind", ""), d), d, odometer, safety)
            desc = d.get("renault_name") or d["description"]
            f.append(_finding(sev, ecu["name"], codes=[d["code"]],
                              title=msg("finding.ecu_code.title", code=d["code"], ftb=ftb, state=d["state"]),
                              detail=msg("finding.ecu_code.detail", desc=desc, ecu=ecu["name"],
                                         tx=ecu["tx"], rx=ecu["rx"], history=_history(d, odometer))))
    if network:
        codes = sorted(network)
        listing = []
        for c in codes:
            extra = [msg("finding.history_times", n=network_times[c])] if c in network_times else []
            if c in network_ago:
                extra.append(msg("finding.history_ago", ago=network_ago[c]))
            listing.append(msg("finding.network_event.code", code=c, modules=network[c] + extra))
        f.append(_finding("low", "network", "network_event", codes=codes,
                          n=len(network_modules), modules=network_modules, listing=listing))
    if silent:
        f.append(_finding("info", "modules", "silent", modules=silent))

    # Modules coded with a different VIN (for example a used part from another car)
    vins = (report.get("vehicle") or {}).get("module_vins") or {}
    if len(set(vins.values())) > 1:
        names = {e["kind"]: e["name"] for e in report.get("ecus", [])}
        listing = [msg("finding.vin_mismatch.item", module=names.get(k, k), vin=v) for k, v in vins.items()]
        f.append(_finding("low", "modules", "vin_mismatch", listing=listing))

    # Modules that measure a lower supply voltage than the OBD socket: a poor ground or connector
    ref = report.get("adapter", {}).get("battery_voltage_end") or report.get("adapter", {}).get("battery_voltage_start")
    low_supply = []
    for ecu in report.get("ecus", []):
        sv = (ecu.get("module_data") or {}).get("supply_voltage")
        if ref and sv is not None and ref - sv > SUPPLY_DROP_V:
            low_supply.append(msg("finding.supply_low.item", module=ecu["name"], v=sv))
    if low_supply:
        f.append(_finding("low", "charging", "supply_low", ref=ref, listing=low_supply))

    # Battery / charging: the guided test, when one was made, else the scan's two readings
    bt = report.get("battery_test")
    if bt:
        f += _battery_test_findings(bt)
    v = None if bt else report.get("adapter", {}).get("battery_voltage_start")
    if v is not None:
        if running:
            if v < 13.0:
                f.append(_finding("high", "charging", "charging_low", v=v))
            elif v > 15.0:
                f.append(_finding("medium", "charging", "charging_high", v=v))
        else:
            if v < 12.0:
                f.append(_finding("medium", "battery", "battery_low", v=v))
            elif v < 12.5:
                f.append(_finding("low", "battery", "battery_slightly_low", v=v))

    if running:
        # Fuel trims (closed loop only)
        fuel_status = (live.get("03") or {}).get("value")
        closed = fuel_status is None or fuel_status in CLOSED_LOOP
        stft, ltft = _val(live, "06"), _val(live, "07", "value")
        if closed and stft is not None and ltft is not None:
            total = stft + ltft
            if abs(total) > 10:
                sev = "high" if abs(total) > 25 else "medium" if abs(total) > 15 else "low"
                f.append(_finding(sev, "fuel", "lean" if total > 0 else "rich",
                                  total=total, stft=stft, ltft=ltft))

        # Coolant / thermostat
        if coolant is not None:
            if coolant > 110:
                f.append(_finding("high", "cooling", "coolant_high", t=coolant))
            elif coolant > 105:
                f.append(_finding("medium", "cooling", "coolant_warm", t=coolant))
            elif runtime is not None and runtime > 900 and coolant < 75:
                f.append(_finding("medium", "cooling", "thermostat", t=coolant, minutes=runtime // 60))

        # O2 sensors
        o2 = live.get("14")
        if o2 and len(o2.get("samples", [])) >= 4 and closed:
            swing = o2["max"] - o2["min"]
            if swing < 0.3:
                f.append(_finding("low", "emissions", "o2_lazy", swing=swing, n=len(o2["samples"])))
        o2d = live.get("15")
        if o2 and o2d and len(o2d.get("samples", [])) >= 4:
            if (o2d["max"] - o2d["min"]) > 0.5 and (o2["max"] - o2["min"]) > 0.5:
                f.append(_finding("low", "emissions", "o2_mirror"))

        # Idle speed
        if speed == 0 and rpm is not None and (rpm < 600 or rpm > 1050) and (coolant or 0) > 75:
            f.append(_finding("low", "engine", "idle", rpm=rpm))

    # Recently cleared codes / readiness
    since = _val(live, "31", "value")
    warmups = _val(live, "30", "value")
    if (since is not None and since < 100) or (warmups is not None and warmups < 5):
        f.append(_finding("medium", "scan", "cleared",
                          km=since if since is not None else "?", warmups=warmups if warmups is not None else "?"))
    incomplete = [m["name"] for m in mon.get("monitors", []) if m["supported"] and not m["complete"]]
    if incomplete:
        f.append(_finding("info", "emissions", "monitors", monitors=incomplete))
    mil_km = _val(live, "21", "value")
    if mil_km:
        f.append(_finding("low", "engine", title=msg("finding.mil_km.title", km=mil_km), detail=""))

    # Mode 06
    for t in obd.get("mode06", []):
        if not t["passed"]:
            f.append(_finding("medium", "emissions", "mode06_fail", monitor=t["monitor"], tid=t["tid"],
                              value=t["value_raw"], min=t["min_raw"], max=t["max_raw"]))
        elif t["margin_pct"] is not None and t["margin_pct"] < 10 and t["max_raw"] != t["min_raw"]:
            f.append(_finding("low", "emissions", "mode06_close", monitor=t["monitor"], tid=t["tid"],
                              margin=t["margin_pct"]))

    f.sort(key=lambda x: SEVERITY_ORDER.index(x["severity"]))
    return f


def summarize(report: dict) -> dict:
    findings = report.get("findings", [])
    counts = {s: sum(1 for x in findings if x["severity"] == s) for s in SEVERITY_ORDER}
    if counts["critical"] or counts["high"]:
        status = "problems"
    elif counts["medium"]:
        status = "attention"
    else:
        status = "good"
    obd = report.get("obd", {})
    ecus = report.get("ecus", [])
    obd_codes = {d["code"] for items in obd.get("dtcs", {}).values() for d in items}
    total_dtcs = sum(len(v) for v in obd.get("dtcs", {}).values()) + sum(
        1 for e in ecus for d in e.get("dtcs", [])
        if not (e.get("tx") == ENGINE_TX and d["code"] in obd_codes))
    return {
        "status": status,
        "severity_counts": counts,
        "total_fault_codes": total_dtcs,
        "modules_responding": sum(1 for e in ecus if e.get("responded")),
        "modules_checked": len(ecus),
        "mil_on": bool((obd.get("monitor_status") or {}).get("mil_on")),
    }
