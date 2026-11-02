import json
import re

from dusterscan.elm327 import ELM327
from dusterscan.i18n import text
from dusterscan.report_html import render_report
from dusterscan.scanner import Scanner
from dusterscan.simulator import SimulatedCar

# A translation key that leaked into rendered text instead of being translated
LEAKED_KEY = re.compile(
    r"\b(?:ui|error|progress|log|finding|report|note|nrc|state|dtc_kind|system|system_letter|severity|"
    r"scan_type|drive|urgency|diy|likelihood|ident|ecu|pid|pidnote|monitor|ignition|mid|ftb|dtc|vehicle|"
    r"fuel_status|fuel_type|obd_std|lang)\.[A-Za-z0-9_]+")


def run_scan(engine_running=True, deep=False, lang="en"):
    car = SimulatedCar(engine_running=engine_running)
    elm = ELM327(car, "SIM", 0)
    elm.initialize()
    report = Scanner(elm, samples=4, lang=lang).run(deep=deep)
    return car, report


def visible_text(html: str) -> str:
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def test_full_scan_against_simulator():
    car, report = run_scan()

    assert car.violations == [], "the scan sent a non-read request"
    assert report["vehicle"]["vin"] == "9FBHSRAD5JM000000"
    assert report["adapter"]["protocol"].startswith("ISO 15765-4 CAN (11 bit")

    obd = report["obd"]
    assert obd["obd_ecus"] == ["7E8", "7E9"]
    assert [d["code"] for d in obd["dtcs"]["stored"]] == ["P0171"]
    assert [d["code"] for d in obd["dtcs"]["pending"]] == ["P0304"]
    assert obd["dtc_read_from"] == ["7E8", "7E9"]
    assert obd["freeze_frame"]["trigger_dtc"] == "P0171"
    assert report["vehicle"]["calibration_ids"] == ["8201535235", "310321146R"]

    # Live data and readiness come from the engine computer, not from the
    # gearbox that answers the same broadcast faster (0 % load, no fuel trims).
    live = {row["pid"]: row for row in obd["live_data"]}
    assert live["04"]["avg"] > 15
    assert "06" in live and "07" in live and "14" in live
    assert text(live["07"]["name"]) == "Long term fuel trim (bank 1)"
    mon = obd["monitor_status"]
    assert mon["mil_on"] and mon["dtc_count"] == 1
    assert next(m for m in mon["monitors"] if m["id"] == "catalyst")["supported"]

    ecus = {e["kind"]: e for e in report["ecus"]}
    assert ecus["engine"]["protocol"] == "KWP2000"
    assert ecus["engine"]["identification"]["part_suffix"] == "0960R"
    assert ecus["engine"]["dtc_read_ok"]            # slow computer: needs the longer timeout
    assert {d["code"] for d in ecus["airbag"]["dtcs"]} == {"B1047"}
    # body lists tests that never failed; only the real code is kept
    assert {d["code"] for d in ecus["body"]["dtcs"]} == {"U0121"}
    assert text(ecus["body"]["dtcs"][0]["failure_type"]) == "missing message"
    assert [d["code"] for d in ecus["abs"]["dtcs"]] == ["C0020", "U3003", "U0100"]
    # gearbox only answers in its extended session
    assert ecus["gearbox"]["protocol"] == "KWP2000" and ecus["gearbox"]["dtc_read_ok"]
    assert ecus["gearbox"]["identification"]["part_suffix"] == "1146R"
    assert ecus["cluster"]["protocol"] == "UDS"
    assert ecus["cluster"]["identification"]["spare_part"] == "248103544R"
    assert not ecus["steering"]["responded"]
    assert all(not e["notes"] for e in report["ecus"] if e["dtc_read_ok"])
    assert report["vehicle"]["module_vins"]["abs"] == "9FBHSRAD5JM000000"

    titles = " | ".join(text(f["title"]) for f in report["findings"])
    assert "Engine running lean" in titles
    assert "Check-engine light" in titles
    assert "Communication / supply-voltage codes stored in 3 module(s)" in titles
    assert "VIN" not in titles  # every module carries the same VIN
    # the engine ECU's direct read repeats the OBD-II codes; they must be reported once
    assert sum("P0171" in f["codes"] for f in report["findings"]) == 1
    assert sum("P0304" in f["codes"] for f in report["findings"]) == 1
    assert report["summary"]["status"] == "problems"
    assert report["summary"]["total_fault_codes"] == 9

    assert report["adapter_log"] and report["adapter_log"][0][1] == "TX"
    assert any(entry[2] == "ATZ" for entry in report["adapter_log"])  # the whole conversation is kept
    assert report["scan_log"][0].startswith("[")
    json.dumps(report)  # report must be serialisable for the AI step and export
    html = render_report(report)
    assert "P0171" in html and "B1047" in html
    assert not LEAKED_KEY.search(visible_text(html))


def test_same_scan_renders_in_spanish():
    _, report = run_scan(lang="es")
    assert any("Leyendo" in line for line in report["scan_log"])
    html = render_report(report, None, "es")
    body = visible_text(html)
    for phrase in ("Informe de salud del vehículo", "Sistema demasiado pobre (banco 1)",
                   "Motor trabajando pobre", "Revoluciones del motor", "falta el mensaje",
                   "Airbag / sistemas de retención", "Se encontraron problemas"):
        assert phrase in body, phrase
    assert not LEAKED_KEY.search(body), LEAKED_KEY.search(body).group(0)


def test_engine_off_scan():
    car, report = run_scan(engine_running=False)
    assert car.violations == []
    titles = [text(f["title"]) for f in report["findings"]]
    assert any("Engine was not running" in t for t in titles)
    assert not any("Engine running lean" in t for t in titles)
    assert "Battery slightly low: 12.4 V (engine off)" in titles


def test_engine_codes_fall_back_to_obd_when_the_direct_read_fails():
    car = SimulatedCar()
    elm = ELM327(car, "SIM", 0)
    elm.initialize()               # (resets the simulated car)
    car.ecus[0x7E0].dtcs = None   # engine refuses 19 02 (and 17 / 18)
    report = Scanner(elm, samples=1).run()
    engine = next(e for e in report["ecus"] if e["kind"] == "engine")
    assert not engine["dtc_read_ok"] and engine["dtc_via_obd"]
    assert not any("Could not read fault codes from Engine" in text(f["title"]) for f in report["findings"])
    assert "via OBD-II" in render_report(report)


def test_deep_scan_finds_nothing_extra_and_stays_read_only():
    car, report = run_scan(deep=True)
    assert car.violations == []
    assert sum(1 for e in report["ecus"] if e["responded"]) == 6
