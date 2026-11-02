from dusterscan import dtc, obd2
from dusterscan.elm327 import ELM327
from dusterscan.i18n import text
from dusterscan.simulator import SimulatedCar


def test_dtc_letters():
    assert obd2.dtc_from_bytes(0x01, 0x71) == "P0171"
    assert obd2.dtc_from_bytes(0x03, 0x04) == "P0304"
    assert obd2.dtc_from_bytes(0x41, 0x23) == "C0123"
    assert obd2.dtc_from_bytes(0x90, 0x47) == "B1047"
    assert obd2.dtc_from_bytes(0xC1, 0x21) == "U0121"


def test_parse_obd_dtcs_can():
    assert obd2.parse_obd_dtcs(bytes([0x43, 0x02, 0x01, 0x71, 0x03, 0x04]), can=True) == ["P0171", "P0304"]
    assert obd2.parse_obd_dtcs(bytes([0x43, 0x00]), can=True) == []


def test_parse_obd_dtcs_kline():
    assert obd2.parse_obd_dtcs(bytes([0x43, 0x01, 0x71, 0x00, 0x00, 0x00, 0x00]), can=False) == ["P0171"]


def test_pid_formulas():
    assert obd2.decode_pid(0x0C, bytes([0x0C, 0x58])) == 790
    assert obd2.decode_pid(0x05, bytes([130])) == 90
    assert obd2.decode_pid(0x07, bytes([128 + 64])) == 50.0
    assert obd2.decode_pid(0x42, (14100).to_bytes(2, "big")) == 14.1
    assert obd2.decode_pid(0x14, bytes([90, 0xFF])) == 0.45


def test_supported_bitmap():
    s = obd2.parse_supported({0x00: bytes([0xBE, 0x3F, 0xB8, 0x13])})
    assert {0x01, 0x03, 0x0C, 0x14, 0x1C, 0x1F, 0x20} <= s
    assert 0x02 not in s and 0x08 not in s


def test_monitor_status():
    st = obd2.parse_monitor_status(bytes([0x81, 0x07, 0x65, 0x21]))
    assert st["mil_on"] and st["dtc_count"] == 1
    cat = next(m for m in st["monitors"] if m["id"] == "catalyst")
    assert cat["supported"] and not cat["complete"]
    assert text(cat["name"], "es") == "Catalizador"


def test_dtc_describe():
    desc, generic = dtc.describe("P0171")
    assert generic and text(desc) == "System too lean (bank 1)"
    assert text(desc, "es") == "Sistema demasiado pobre (banco 1)"
    assert text(dtc.describe("P1234")[0]).startswith("Manufacturer-specific")
    assert text(dtc.describe("B1047")[0], "es") == "Código específico del fabricante (carrocería)"
    assert text(dtc.describe("P0999")[0]).startswith("Generic powertrain code")
    assert text(dtc.failure_type(0x87), "es") == "falta el mensaje"
    assert text(dtc.failure_type(0xEE)) == "type 0xEE"


def test_text_values_are_translated():
    assert text(obd2.decode_pid(0x03, bytes([2, 0])), "es") == "lazo cerrado"
    assert text(obd2.decode_pid(0x1C, bytes([6])), "en") == "EOBD (Europe)"
    assert text(obd2.decode_pid(0x51, bytes([99]))) == "type 0x63"
    assert text(obd2.PIDS[0x15].name, "es") == "Voltaje del sensor de oxígeno (banco 1, sensor 2)"


def test_isotp_reassembly_with_headers():
    elm = ELM327(SimulatedCar())
    elm.protocol_num, elm.headers_on = "6", True
    msgs = elm._parse_can([
        "7E8 10 14 49 02 01 39 46 42",
        "7E8 21 48 53 52 41 44 35 4A",
        "7E8 22 4D 30 30 30 30 30 30",
    ])
    assert len(msgs) == 1 and msgs[0].header == "7E8"
    assert obd2.parse_vin(msgs[0].data) == "9FBHSRAD5JM000000"


def test_plain_multiframe():
    elm = ELM327(SimulatedCar())
    msgs = elm._parse_plain(["014", "0: 49 02 01 39 46 42", "1: 48 53 52 41 44 35 4A", "2: 4D 30 30 30 30 30 30"])
    assert obd2.parse_vin(msgs[0].data) == "9FBHSRAD5JM000000"
