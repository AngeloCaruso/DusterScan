"""Invented module definitions in DDT4All's format, for the simulated car.

Used by demo mode and the tests so the database features can be tried without
Renault's files. None of this is Renault data. Each definition also contains
write requests (and other requests DusterScan must never send) so the tests can
prove they are filtered out.
"""

from __future__ import annotations

PROJECT = "x79Ph2"


def _reply_header(sent: str) -> str:
    sid = int(sent[:2], 16)
    id_len = {0x22: 3, 0x21: 2, 0x19: 2}.get(sid, 1)
    return f"{sid + 0x40:02X}" + sent[2:id_len * 2]


def _req(name: str, sent: str, items: dict, minbytes: int, **extra) -> dict:
    r = {"name": name, "sentbytes": sent, "replybytes": _reply_header(sent).ljust(minbytes * 2, "0"),
         "minbytes": minbytes, "deny_sds": [], "receivebyte_dataitems": items}
    r.update(extra)
    return r


def _write(name: str, sent: str, item: str) -> dict:
    return {"name": name, "sentbytes": sent, "replybytes": f"{int(sent[:2], 16) + 0x40:02X}", "minbytes": 1,
            "deny_sds": [], "manualsend": True, "sendbyte_dataitems": {item: {"firstbyte": 4}}}


def _dtc_records(*records: tuple[str, str]) -> list[dict]:
    out = [_req("ReadDTCInformation.ReportDTC", "1902FF", {"StatusOfDTC": {"firstbyte": 7}}, 3,
                shiftbytescount=4)]
    for rec, item in records:
        out.append(_req(f"ReadDTCInformation.ReportExtendedData.{item}", f"1906000000{rec}",
                        {"DTCRecord": {"firstbyte": 3}, "StatusOfDTC": {"firstbyte": 6},
                         "DTCExtendedDataRecordNumber": {"firstbyte": 7},
                         f"DTCExtendedData.{item}": {"firstbyte": 8}}, 8,
                        sendbyte_dataitems={"DTCMaskRecord": {"firstbyte": 3}}))
    return out


_DTC_DATA = {
    "StatusOfDTC": {"byte": True},
    "DTCRecord": {"bitscount": 24, "byte": True, "bytescount": 3},
    "DTCMaskRecord": {"bitscount": 24, "byte": True, "bytescount": 3},
    "DTCExtendedDataRecordNumber": {"byte": True},
    "DTCExtendedData.Mileage": {"bitscount": 24, "scaled": True, "bytescount": 3, "unit": "km"},
    "DTCExtendedData.DTCOccurrenceCounter": {"scaled": True},
}
_MILEAGE_AND_COUNT = (("80", "Mileage"), ("82", "DTCOccurrenceCounter"))


def _definition(tx: str, rx: str, addr: str, requests: list, data: dict, devices=(), screens=None) -> dict:
    d = {"ecuname": "", "obd": {"protocol": "CAN", "send_id": tx, "recv_id": rx, "funcaddr": addr},
         "endian": "Big", "requests": requests, "data": {**_DTC_DATA, **data},
         "devices": [{"name": n, "dtc": c, "dtctype": 0} for c, n in devices]}
    layout = {"categories": {}, "screens": {}}
    for category, screen, displays in screens or []:
        layout["categories"].setdefault(category, []).append(screen)
        layout["screens"][screen] = {"displays": [{"text": item, "request": req} for req, item in displays]}
    return d, layout


def _engine():
    reqs = _dtc_records(*_MILEAGE_AND_COUNT) + [
        _req("DataRead.Battery voltage", "222001", {"Battery voltage": {"firstbyte": 4}}, 4),
        _req("DataRead.Coolant temperature", "222002", {"Coolant temperature": {"firstbyte": 4}}, 4),
        _req("DataRead.Idle speed setpoint", "222003", {"Idle speed setpoint": {"firstbyte": 4}}, 5),
        _req("DataRead.AC request", "222004", {"AC request": {"firstbyte": 4, "bitoffset": 7}}, 4),
        _req("DataRead.Engine type", "222005", {"Engine type": {"firstbyte": 4}}, 6),
        _req("DataRead.Learnt values", "222006", {"Learnt values": {"firstbyte": 4}}, 4, manualsend=True),
        _req("DataRead.Factory data", "222007", {"Factory data": {"firstbyte": 4}}, 4, deny_sds=["nosds"]),
        _write("DataWrite.Idle speed setpoint", "2E200300", "Idle speed setpoint"),
        _write("RoutineControl.Injector test", "3101FF00", "Idle speed setpoint"),
        _write("ClearDiagnosticInformation", "14FFFFFF", "DTCMaskRecord"),
        _write("ECUReset", "1101", "DTCMaskRecord"),
    ]
    data = {
        "Battery voltage": {"scaled": True, "step": 0.1, "unit": "V", "format": "0.0"},
        "Coolant temperature": {"scaled": True, "offset": -40, "unit": "Â°C"},  # mojibake on purpose
        "Idle speed setpoint": {"bytescount": 2, "scaled": True, "unit": "rpm"},
        "AC request": {"bitscount": 1, "lists": {"0": "0b = No", "1": "1b = Yes"}},
        "Engine type": {"bytescount": 3, "bytesascii": True},
        "Learnt values": {"byte": True},
        "Factory data": {"byte": True},
    }
    screens = [("Lecture", "Engine measures", [("DataRead.Battery voltage", "Battery voltage"),
                                               ("DataRead.Coolant temperature", "Coolant temperature"),
                                               ("DataRead.Idle speed setpoint", "Idle speed setpoint"),
                                               ("DataRead.AC request", "AC request")]),
               ("Configuration", "Engine configuration", [("DataRead.Engine type", "Engine type")])]
    return _definition("7E0", "7E8", "7A", reqs, data,
                       [(0x0171, "Fuel mixture adaptation too lean"), (0x0304, "Cylinder 4 misfire")], screens)


def _gearbox():
    reqs = [
        _req("DataRead.Gearbox measures", "2101", {"Gearbox oil temperature": {"firstbyte": 3},
                                                   "Selected gear": {"firstbyte": 4, "bitoffset": 4}}, 4),
        _req("DataRead.Line pressure", "2102", {"Line pressure": {"firstbyte": 3}}, 3),
        _write("WriteDataByLocalIdentifier.Adaptives", "3B0100", "Line pressure"),
    ]
    data = {"Gearbox oil temperature": {"scaled": True, "offset": -40, "unit": "°C"},
            "Selected gear": {"bitscount": 4, "lists": {"0": "P", "1": "R", "2": "N", "3": "D"}},
            "Line pressure": {"scaled": True, "step": 0.1, "unit": "bar"}}
    screens = [("Lecture", "Gearbox measures", [("DataRead.Gearbox measures", "Gearbox oil temperature"),
                                                ("DataRead.Gearbox measures", "Selected gear"),
                                                ("DataRead.Line pressure", "Line pressure")])]
    return _definition("7E1", "7E9", "6E", reqs, data, (), screens)


def _abs():
    reqs = _dtc_records(*_MILEAGE_AND_COUNT) + [
        _req("DataRead.Front left wheel speed", "224B01", {"Front left wheel speed": {"firstbyte": 4}}, 5),
        _req("DataRead.Supply voltage", "224B02", {"Supply voltage": {"firstbyte": 4}}, 4),
        _write("InputOutputControl.Pump motor", "300101", "Supply voltage"),
    ]
    data = {"Front left wheel speed": {"bytescount": 2, "scaled": True, "step": 0.01, "unit": "km/h"},
            "Supply voltage": {"scaled": True, "step": 0.1, "unit": "V"}}
    screens = [("Lecture", "Wheel speeds and supply", [("DataRead.Front left wheel speed", "Front left wheel speed"),
                                                       ("DataRead.Supply voltage", "Supply voltage")])]
    return _definition("740", "760", "28", reqs, data,
                       [(0xF003, "Supply voltage"), (0xC100, "Lost communication with ECM"),
                        (0x4020, "ABS pump motor")], screens)


def _cluster():
    reqs = [_req("DataRead.Fuel level", "220201", {"Fuel level": {"firstbyte": 4}}, 4)]
    data = {"Fuel level": {"scaled": True, "unit": "%"}}
    return _definition("743", "763", "51", reqs, data, (), [("Lecture", "Cluster", [("DataRead.Fuel level",
                                                                                      "Fuel level")])])


def _body():
    reqs = _dtc_records(*_MILEAGE_AND_COUNT) + [
        _req("DataRead.Battery voltage", "220101", {"Battery voltage": {"firstbyte": 4}}, 4),
        _req("DataRead.Driver door", "220102", {"Driver door": {"firstbyte": 4, "bitoffset": 7},
                                                "Not used 1": {"firstbyte": 4}}, 4),
        _req("DataRead.Total mileage", "220103", {"Total mileage": {"firstbyte": 4}}, 6),
        _write("DataWrite.Total mileage", "2E0103000000", "Total mileage"),
    ]
    data = {"Battery voltage": {"scaled": True, "step": 0.1, "unit": "V"},
            "Driver door": {"bitscount": 1, "lists": {"0": "Closed", "1": "Open"}},
            "Not used 1": {"bitscount": 7},
            "Total mileage": {"bytescount": 3, "scaled": True, "unit": "km"}}
    screens = [("Lecture", "Body measures", [("DataRead.Battery voltage", "Battery voltage"),
                                             ("DataRead.Driver door", "Driver door"),
                                             ("DataRead.Total mileage", "Total mileage")])]
    return _definition("745", "765", "26", reqs, data, [(0xC121, "Lost communication with ABS")], screens)


def _airbag():
    reqs = _dtc_records(*_MILEAGE_AND_COUNT) + [
        _req("Reading of the firing lines", "2130", {"Driver airbag resistance": {"firstbyte": 3},
                                                     "Passenger airbag resistance": {"firstbyte": 4}}, 4),
        _write("Firing line configuration", "3B3001", "Driver airbag resistance"),
    ]
    data = {"Driver airbag resistance": {"scaled": True, "step": 0.1, "unit": "ohm", "format": "0.0"},
            "Passenger airbag resistance": {"scaled": True, "step": 0.1, "unit": "ohm", "format": "0.0"}}
    screens = [("Configuration", "Firing lines", [("Reading of the firing lines", "Driver airbag resistance"),
                                                  ("Reading of the firing lines", "Passenger airbag resistance")])]
    return _definition("752", "772", "2C", reqs, data, [(0x9047, "Driver pretensioner circuit")], screens)


# file name -> (builder, db.json entry). Identifications match simulator.py's modules:
# engine/gearbox/airbag exact, body same software, ABS same family only, cluster none.
_FILES = {
    "DEMO_EMS_Engine_V1.1_20200116T154114.json": (_engine, "7A", [("68", "S3S", "00DE", "4500")]),
    "DEMO_DP0_Gearbox_v7.5_20161104T143902.json": (_gearbox, "6E", [("7", "AIW", "0042", "0102")]),
    "DEMO_ABS_ESC_Version_4.3_20210827T173242.json": (_abs, "28", [("8", "037", "7692", "0181")]),
    "DEMO_CLUSTER_v3.3_20160120T132838.json": (_cluster, "51", [("13", "39324d", "0034", "0703")]),
    "DEMO_BCM_T4_16.5_20171221T171512.json": (_body, "26", [("5", "001", "1419", "1020")]),
    "DEMO_ACU_Airbag_v15_20150511T131328.json": (_airbag, "2C", [("4", "AMR", "1630", "1630")]),
}


def database() -> tuple[dict, dict]:
    """(db.json content, {member name: content}) as in DDT4All's ecu.zip."""
    db, files = {}, {}
    for name, (build, addr, idents) in _FILES.items():
        definition, layout = build()
        definition["ecuname"] = name.rsplit("_", 1)[0]
        files[name] = definition
        files[name + ".layout"] = layout
        db[name] = {"address": addr, "group": "", "protocol": "CAN", "projects": [PROJECT],
                    "ecuname": definition["ecuname"],
                    "autoidents": [{"diagnostic_version": d, "supplier_code": s, "soft_version": sw, "version": v}
                                   for d, s, sw, v in idents]}
    return db, files
