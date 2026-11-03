"""Renault module database: decoding, read-only filtering, import, matching, scan enrichment."""

import json
import re
import zipfile

import pytest

from dusterscan import rules, safety, settings
from dusterscan.ddt import match, store
from dusterscan.ddt.decode import decode_item, decode_request, reply_ok
from dusterscan.ddt.reader import ModuleReader, dtc_record_requests, enrich_module, readable_requests
from dusterscan.elm327 import ELM327
from dusterscan.i18n import msg, text
from dusterscan.renault import parse_renault_ident
from dusterscan.report_html import render_report
from dusterscan.scanner import Scanner
from dusterscan.simulator import SimulatedCar

from tests.test_real_duster import IDENTS
from tests.test_scan import LEAKED_KEY, visible_text

WRITE_SIDS = {0x04, 0x08, 0x11, 0x14, 0x23, 0x27, 0x28, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x34, 0x35, 0x36, 0x37,
              0x3B, 0x3D, 0x85}


# -- decoding --------------------------------------------------------------------

def test_decoding_follows_ddt4all_rules():
    reply = bytes.fromhex("59020000002B")   # a 19 02 answer: status 0x2B in byte 6
    one_bit = {"bitscount": 1, "lists": {"0": "No", "1": "Yes"}}
    # bit offset counts from the most significant bit: offset 7 = bit 0 (UDS "testFailed")
    assert decode_item(one_bit, {"firstbyte": 6, "bitoffset": 7}, reply) == "Yes"
    assert decode_item(one_bit, {"firstbyte": 6, "bitoffset": 5}, reply) == "No"    # bit 2
    assert decode_item(one_bit, {"firstbyte": 6, "bitoffset": 2}, reply) == "Yes"   # bit 5
    word = bytes.fromhex("6220031F40")
    assert decode_item({"bytescount": 2, "scaled": True, "unit": "rpm"}, {"firstbyte": 4}, word) == 8000
    assert decode_item({"bytescount": 2, "scaled": True}, {"firstbyte": 4, "endian": "Little"}, word) == 0x401F
    assert decode_item({"scaled": True, "offset": -40}, {"firstbyte": 4}, bytes.fromhex("62200282")) == 90
    assert decode_item({"scaled": True, "step": 0.1, "format": "0.0"}, {"firstbyte": 4},
                       bytes.fromhex("6220018D")) == 14.1
    assert decode_item({"scaled": True, "divideby": 4}, {"firstbyte": 2}, bytes.fromhex("6106")) == 1.5
    assert decode_item({"scaled": True, "signed": True}, {"firstbyte": 2}, bytes.fromhex("61FE")) == -2
    assert decode_item({"bitscount": 2, "lists": {"0": "00b = Automatic gearbox"}}, {"firstbyte": 2, "bitoffset": 6},
                       bytes.fromhex("6100")) == "Automatic gearbox"   # the "00b = " code prefix is dropped
    assert decode_item({"bytescount": 3, "bytesascii": True}, {"firstbyte": 4}, bytes.fromhex("622005463452")) == "F4R"
    assert decode_item({"bytescount": 3, "byte": True}, {"firstbyte": 3}, bytes.fromhex("5906017100")) == "017100"
    # an item outside the reply is dropped, never guessed
    assert decode_item({"bytescount": 2, "scaled": True}, {"firstbyte": 4}, bytes.fromhex("622003")) is None


def test_replies_must_fit_the_request():
    req = {"sentbytes": "222001", "minbytes": 4, "receivebyte_dataitems": {"V": {"firstbyte": 4}}}
    definition = {"data": {"V": {"scaled": True}}, "requests": [req]}
    assert reply_ok(req, bytes.fromhex("6220018D"))
    assert decode_request(definition, req, bytes.fromhex("6220018D")) == {"V": 141}
    assert decode_request(definition, req, bytes.fromhex("6220028D")) == {}   # another identifier
    assert decode_request(definition, req, bytes.fromhex("622001")) == {}     # too short
    assert decode_request(definition, req, bytes.fromhex("7F2231")) == {}     # refused


def test_text_repair():
    assert store.repair("Â°C") == "°C"
    assert store.repair("Configuration GÃ©nÃ©rale") == "Configuration Générale"
    assert store.repair("plain") == "plain"


# -- read-only ---------------------------------------------------------------------

def demo_definitions():
    db = store.demo()
    return [(name, db.definition(name)) for name in db.entries]


def test_only_reads_are_taken_from_a_definition():
    seen_writes = 0
    for name, d in demo_definitions():
        for r in readable_requests(d):
            assert int(r["sentbytes"][:2], 16) in (0x21, 0x22), (name, r["name"])
            assert not r.get("manualsend") and not r.get("deny_sds") and not r.get("sendbyte_dataitems")
            safety.check_command(r["sentbytes"])  # never raises for what the reader may send
        for r in dtc_record_requests(d):
            assert r["sentbytes"].startswith("1906") and len(r["sentbytes"]) == 12
        for r in d["requests"]:
            if int(r["sentbytes"][:2], 16) in WRITE_SIDS:
                seen_writes += 1
                assert r not in readable_requests(d)
                with pytest.raises(safety.WriteBlockedError):
                    safety.check_command(r["sentbytes"])
    assert seen_writes >= 8  # the demo definitions do contain writes, like the real ones
    engine = dict(demo_definitions())["DEMO_EMS_Engine_V1.1_20200116T154114.json"]
    names = {r["name"] for r in readable_requests(engine)}
    assert "DataRead.Learnt values" not in names     # marked for manual sending
    assert "DataRead.Factory data" not in names      # restricted to special sessions


# -- import ------------------------------------------------------------------------

def test_import_validates_and_indexes(tmp_path):
    assert store.status() is None and store.load() is None
    with pytest.raises(store.DatabaseError) as err:
        store.import_zip(_write(tmp_path / "x.txt", b"hi"))
    assert err.value.key == "error.ddt_not_zip"
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(other, "w") as z:
        z.writestr("readme.txt", "not a database")
    with pytest.raises(store.DatabaseError) as err:
        store.import_zip(other)
    assert err.value.key == "error.ddt_not_ddt"

    progress = []
    info = store.import_zip(store.write_demo_zip(tmp_path / "ecu.zip"), progress.append)
    assert info["count"] == 6 and info["source_name"] == "ecu.zip"
    assert progress[-1] == 1.0
    db = store.load()
    assert db.can_ids("7A") == ("7E0", "7E8") and db.can_ids("26") == ("745", "765")
    assert db.definition("DEMO_ACU_Airbag_v15_20150511T131328.json")["obd"]["send_id"] == "752"
    assert db.layout("DEMO_ACU_Airbag_v15_20150511T131328.json")["screens"]
    store.remove()
    assert store.status() is None


def _write(path, data: bytes):
    path.write_bytes(data)
    return path


# -- matching ----------------------------------------------------------------------------

def real_index() -> store.Database:
    """The identification values DDT4All's index lists for the files that matter for a 2018 Duster."""
    db = {
        "EMS312X_RDC_xxx_RDE_320_470_5C0_600_760_850_V2.0_20180806T094037.json": ("7A", [("68", "001", "00DE", "4500")]),
        "EMS312X_RDC_xxx_RDE_320_470_5F0_600_790_850_V1.1_20200116T154114.json": ("7A", [("68", "001", "00DE", "4500"),
                                                                                         ("70", "001", "00DE", "4500")]),
        "CLUSTER_X52_2nd_Indus_v3.3_20160120T132838.json": ("51", [("13", "39324d", "0034", "0703")]),
        "RSAT4_ACU_eng_v15_20150511T131328.json": ("2C", [("4", "AMR", "1630", "1630")]),
        "BCM_T4_VS_DDT_16.5_20171221_20171221T171512.json": ("26", [("5", "001", "1419", "1020")]),
        "BCM_T4_VS_DDT_19_v3_20151027T105308.json": ("26", [("5", "001", "1419", "1000")]),
        "ABSESC_X10527998_Version_4.3_20210827T173242.json": ("01", [("8", "037", "7692", "0181")]),
        "ABSESC___X79_Gen_9.0_Version_1.0_20141022T135545.json": ("01", [("8", "037", "1335", "0410")]),
    }
    entries = store._entries({n: {"address": a, "protocol": "CAN", "projects": ["x79Ph2"], "ecuname": n[:-5],
                                  "autoidents": [{"diagnostic_version": d, "supplier_code": s, "soft_version": sw,
                                                  "version": v} for d, s, sw, v in ids]}
                              for n, (a, ids) in db.items()})
    can = {"7A": ["7E0", "7E8"], "51": ["743", "763"], "2C": ["752", "772"], "26": ["745", "765"], "01": ["740", "760"]}
    return store.Database({"count": len(entries)}, entries, can, lambda m: None)


def real_ecu(kind: str, tx: str, rx: str) -> dict:
    return {"kind": kind, "tx": tx, "rx": rx, "responded": True,
            "identification": parse_renault_ident(bytes.fromhex(IDENTS[kind]))}


def test_the_real_duster_matches_as_found_by_hand():
    db = real_index()
    engine, cluster = real_ecu("engine", "7E0", "7E8"), real_ecu("cluster", "743", "763")
    airbag, body, abs_ = real_ecu("airbag", "752", "772"), real_ecu("body", "745", "765"), real_ecu("abs", "740", "760")
    # newest of the identical engine definitions; the cluster's hex-coded index values still match
    assert match.auto_choice(db, engine) == ("EMS312X_RDC_xxx_RDE_320_470_5F0_600_790_850_V1.1_20200116T154114.json", "exact")
    assert match.auto_choice(db, cluster) == ("CLUSTER_X52_2nd_Indus_v3.3_20160120T132838.json", "exact")
    assert match.auto_choice(db, airbag) == ("RSAT4_ACU_eng_v15_20150511T131328.json", "exact")
    # same software, edition 1030 vs 1020 (closest file first)
    assert match.auto_choice(db, body) == ("BCM_T4_VS_DDT_16.5_20171221_20171221T171512.json", "same_software")
    # ABS software 4569 is in no file: stays generic, but the dropdown offers the family
    assert match.auto_choice(db, abs_) is None
    offered = match.candidates(db, abs_)["items"]
    assert [i["tier"] for i in offered] == ["same_family", "same_family"]
    assert offered[0]["file"].startswith("ABSESC_X10527998_Version_4.3")   # newest first
    assert match.vehicle_project(db, [engine, cluster, airbag, body, abs_]) == "x79Ph2"
    assert match.candidates(db, abs_, query="gen 9.0")["items"][0]["file"].startswith("ABSESC___X79_Gen_9.0")


def test_user_choice_wins_and_is_remembered_per_identification():
    db = real_index()
    abs_ = real_ecu("abs", "740", "760")
    key = match.module_key(abs_)
    assert key == "740|8|037|4569|0170"
    pick = "ABSESC_X10527998_Version_4.3_20210827T173242.json"
    assert match.resolve(db, abs_, {key: pick}) == {"file": pick, "tier": "same_family", "chosen": "user"}
    body = real_ecu("body", "745", "765")
    assert match.resolve(db, body, {match.module_key(body): "generic"}) is None
    # a replaced module (other software) does not inherit the old choice
    other = dict(abs_, identification=dict(abs_["identification"], software="4570"))
    assert match.resolve(db, other, {key: pick}) is None


# -- reading -------------------------------------------------------------------------------

def connected():
    car = SimulatedCar()
    elm = ELM327(car, "SIM", 0)
    elm.initialize()
    elm.set_timeout_ms(1000)
    return car, elm


def sim_ecu(kind: str, tx: str, rx: str, protocol: str = "KWP2000") -> dict:
    car, elm = connected()
    elm.set_target(int(tx, 16), int(rx, 16))
    pos = next(m for m in elm.request("2180" if protocol == "KWP2000" else "22F187") if m.data[0] in (0x61, 0x62))
    ecu = {"kind": kind, "name": msg(f"ecu.{kind}"), "tx": tx, "rx": rx, "responded": True, "protocol": protocol,
           "identification": parse_renault_ident(pos.data) if protocol == "KWP2000" else {}, "dtcs": [], "notes": []}
    return ecu


def test_a_definition_that_does_not_fit_is_dropped():
    car, elm = connected()
    body = sim_ecu("body", "745", "765")
    body["dtcs"] = [{"code": "U0121", "raw": "C12187", "state_id": "stored"}]
    defs = store.demo()
    # the cluster's definition is for another CAN address
    enrich_module(elm, body, defs, {"file": "DEMO_CLUSTER_v3.3_20160120T132838.json", "tier": "same_address",
                                    "chosen": "user"})
    assert body["definition"]["status"] == "rejected" and "module_data" not in body
    assert text(body["definition"]["reason"]) == "the definition is for another CAN address"
    # a definition whose requests the module refuses
    bogus = json.loads(json.dumps(defs.definition("DEMO_BCM_T4_16.5_20171221T171512.json")))
    bogus["requests"] = [dict(r, sentbytes=f"22{0x3000 + i:04X}", name=f"DataRead.X{i}") for i, r in
                         enumerate(r for r in bogus["requests"] if r["sentbytes"].startswith("22")) for _ in range(4)]
    db = store.Database({}, {"BOGUS.json": defs.entries["DEMO_BCM_T4_16.5_20171221T171512.json"]},
                        {"26": ("745", "765")}, lambda m: json.dumps(bogus).encode() if m == "BOGUS.json" else None)
    enrich_module(elm, body, db, {"file": "BOGUS.json", "tier": "same_software", "chosen": "auto"})
    assert body["definition"]["status"] == "rejected"
    assert "renault_name" not in body["dtcs"][0]   # names from a rejected definition are removed too
    assert car.violations == []


def test_standard_scan_budget_reads_the_most_informative_first():
    car, elm = connected()
    engine = sim_ecu("engine", "7E0", "7E8")
    reader = ModuleReader(elm, engine, store.demo(), {"file": "DEMO_EMS_Engine_V1.1_20200116T154114.json",
                                                      "tier": "exact", "chosen": "auto"})
    data = reader.read_values(budget_s=-1)
    assert data["partial"] and data["read"] == 0
    full = reader.read_values(budget_s=None)
    assert not full["partial"] and full["read"] == full["total"] == 5
    order = [v["item"] for g in full["groups"] for v in g["values"]]
    assert order.index("Battery voltage") < order.index("Engine type")   # measurements before configuration


def test_scan_with_the_demo_database():
    car, elm = connected()
    report = Scanner(elm, samples=1, ddt=store.demo()).run()
    assert car.violations == []
    for never in ("222006", "222007", "2E200300", "3101FF00", "14FFFFFF", "1101", "3B0100", "300101", "3B3001"):
        assert never not in car.received, never
    ecus = {e["kind"]: e for e in report["ecus"]}
    assert report["ddt"]["matched"] == 4 and report["ddt"]["project"] == "x79Ph2"
    assert {k: (e.get("definition") or {}).get("tier") for k, e in ecus.items() if e["responded"]} == {
        "engine": "exact", "gearbox": "exact", "airbag": "exact", "body": "same_software", "abs": None, "cluster": None}
    # fault codes: Renault names, mileage and occurrences; generic modules untouched
    p0171 = next(d for d in ecus["engine"]["dtcs"] if d["code"] == "P0171")
    assert p0171["renault_name"] == "Fuel mixture adaptation too lean"
    assert p0171["extended"] == {"mileage_km": 89420, "occurrences": 3}
    assert all("renault_name" not in d for d in ecus["abs"]["dtcs"])
    # module values, the gearbox's only in its extended session
    gear = {v["item"]: v["value"] for g in ecus["gearbox"]["module_data"]["groups"] for v in g["values"]}
    assert gear == {"Gearbox oil temperature": 75, "Selected gear": "D", "Line pressure": 4.2}
    assert any("extended diagnostic session" in line for line in report["scan_log"])
    coolant = next(v for g in ecus["engine"]["module_data"]["groups"] for v in g["values"] if v["item"] == "Coolant temperature")
    assert coolant["unit"] == "°C"   # repaired from the file's "Â°C"
    assert report["meta"]["odometer_km"] == 90000 and report["meta"]["odometer_source"] == "body"

    titles = " | ".join(text(f["title"]) for f in report["findings"])
    details = " | ".join(text(f["detail"]) for f in report["findings"])
    assert "A module measures a lower supply voltage than the OBD socket" in titles   # body 13.4 V vs 14.1 V
    assert "Renault name: Fuel mixture adaptation too lean" in details
    assert "Recorded at 89,420 km (580 km ago), 3×" in details
    airbag = next(f for f in report["findings"] if "B1047" in f["codes"])
    assert airbag["severity"] == "low"   # one occurrence, 28,800 km ago: lowered from medium

    html = render_report(report)
    body = visible_text(html)
    assert "Module data (Renault definitions)" in body and "Gearbox oil temperature" in body
    assert "Driver pretensioner circuit" in body and "at 61,200 km (28,800 km ago)" in body
    assert not LEAKED_KEY.search(body), LEAKED_KEY.search(body).group(0)
    es = visible_text(render_report(report, None, "es"))
    assert "Datos de módulos" in es and not LEAKED_KEY.search(es)


def test_scan_without_a_database_is_unchanged():
    car, elm = connected()
    report = Scanner(elm, samples=1).run()
    assert "ddt" not in report
    assert not any(k in e for e in report["ecus"] for k in ("definition", "module_data", "module_raw"))
    assert not any("renault_name" in d for e in report["ecus"] for d in e["dtcs"])


def test_app_choice_without_the_car_applies_names_only(monkeypatch):
    from dusterscan.app import Api

    car, elm = connected()
    report = Scanner(elm, samples=1, ddt=store.demo()).run()
    api = Api()
    api._report, api._report_live = report, False      # a saved scan, opened later
    monkeypatch.setattr(api, "_autosave", lambda: "")
    monkeypatch.setattr(api, "_lang", lambda: "en")
    rows = {r["tx"]: r for r in api.ddt_modules()["rows"]}
    assert rows["740"]["status"] is None and rows["7E0"]["tier"] == "exact"
    cands = api.ddt_candidates("740")["items"]
    assert cands[0]["file"] == "DEMO_ABS_ESC_Version_4.3_20210827T173242.json" and cands[0]["tier"] == "same_family"
    assert api.ddt_choose("740", cands[0]["file"]) == {"ok": True, "reading": False}
    abs_ = api._find_ecu("740")
    assert abs_["definition"]["chosen"] == "user"
    assert {d["code"]: d.get("renault_name") for d in abs_["dtcs"]} == {
        "C0020": "ABS pump motor", "U3003": "Supply voltage", "U0100": "Lost communication with ECM"}
    # the simulated car copies a real Duster's identifications: its picks never touch the real car's
    assert settings.load()["ddt_choices"] == {"demo|740|8|037|4569|0170": cands[0]["file"]}
    assert api._choices() == {"740|8|037|4569|0170": cands[0]["file"]}
    assert api.ddt_choose("740", "generic")["ok"]
    assert "definition" not in abs_ and all("renault_name" not in d for d in abs_["dtcs"])


def test_odometer_from_settings_and_the_car(monkeypatch):
    from dusterscan.app import Api

    api = Api()
    monkeypatch.setattr(api, "_lang", lambda: "en")
    state = api.save_settings(None, "90.000")          # typed with a thousands separator
    assert state["odometer_km"] == 90000 and state["odometer_at"]
    assert api.save_settings(None, None)["odometer_km"] == 90000   # None = unchanged
    # without the database the typed mileage is used ...
    car, elm = connected()
    meta = Scanner(elm, samples=1, odometer_km=95000).run()["meta"]
    assert (meta["odometer_km"], meta["odometer_source"]) == (95000, "user")
    # ... but the car's own reading wins: a number typed weeks ago may be stale
    car, elm = connected()
    meta = Scanner(elm, samples=1, ddt=store.demo(), odometer_km=95000).run()["meta"]
    assert (meta["odometer_km"], meta["odometer_source"]) == (90000, "body")
    assert api.save_settings(None, "")["odometer_km"] is None   # cleared
