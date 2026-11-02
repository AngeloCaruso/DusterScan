"""Replays answers recorded from a real 2018 Duster (vLinker FS, first scans on 2026-11-02).

Those scans reported 83 fault codes when the car had 10: the modules list
every test they know with its status, and most had only the "not completed"
bits set. These tests pin the decoding to the real bytes.
"""

from dusterscan import rules
from dusterscan.elm327 import ELM327
from dusterscan.i18n import msg, text
from dusterscan.renault import EcuResult, EcuScanner, parse_renault_ident

# Answers to "19 02 FF", as the adapter printed them (headers on, CAN).
ABS_1902 = (
    "760 10 73 59 02 7B 40 20 04 \r760 21 50 40 20 1C 68 C4 22 \r760 22 00 28 40 40 64 50 C4 \r"
    "760 23 01 00 28 C1 00 00 28 \r760 24 50 50 04 50 F0 03 17 \r760 25 40 51 56 55 50 51 89 \r"
    "760 26 64 50 F0 03 16 28 C0 \r760 27 01 00 50 F0 03 13 28 \r760 28 51 25 95 50 40 31 1D \r"
    "760 29 40 40 31 38 50 40 30 \r760 2A 97 40 40 34 1D 40 40 \r760 2B 34 38 50 40 33 97 40 \r"
    "760 2C 51 27 64 50 51 26 95 \r760 2D 50 40 37 1D 40 40 37 \r760 2E 38 50 40 36 97 40 40 \r"
    "760 2F 3A 1D 40 40 3A 38 50 \r760 20 40 39 97 40 00 00 00")
CLUSTER_1902 = "763 10 13 59 02 39 C0 01 87 \r763 21 28 94 04 13 20 F0 03 \r763 22 16 28 94 12 11 20 00"
BODY_1902 = (
    "765 10 CF 59 02 B9 95 04 14 \r765 21 10 95 10 14 10 95 11 \r765 22 14 10 95 12 14 10 95 \r"
    "765 23 13 14 10 95 14 14 10 \r765 24 95 50 12 10 95 50 14 \r765 25 10 95 F1 08 10 95 71 \r"
    "765 26 01 10 95 71 15 10 95 \r765 27 71 11 10 95 28 19 10 \r765 28 95 29 19 10 95 82 46 \r"
    "765 29 10 95 02 43 10 95 B2 \r765 2A 24 10 95 28 18 10 95 \r765 2B 29 18 10 95 60 23 10 \r"
    "765 2C 95 60 24 10 95 61 23 \r765 2D 10 95 62 23 10 95 62 \r765 2E 24 10 95 A0 64 10 95 \r"
    "765 2F 19 23 10 95 19 24 10 \r765 20 95 68 04 10 95 82 42 \r765 21 10 5A 60 7B 10 5A 80 \r"
    "765 22 54 10 5A 80 55 10 5A \r765 23 82 68 10 5A 83 68 10 \r765 24 5A 84 68 10 5A 85 68 \r"
    "765 25 10 5A 72 7A 10 5A 73 \r765 26 7A 10 5A 74 7A 10 5A \r765 27 75 7A 10 5A 86 1C 10 \r"
    "765 28 5A 86 55 10 5A 86 96 \r765 29 10 92 0F 16 10 92 0F \r765 2A 17 10 92 3A 92 10 92 \r"
    "765 2B 3A 96 10 92 66 81 10 \r765 2C 92 66 96 10 95 05 01 \r765 2D 10 95 72 29 10 FF FF")

# "61 80" identification answers
IDENTS = {
    "engine": "61 80 38 32 30 30 52 44 30 30 31 34 39 35 33 52 00 DE 45 00 45 70 01 01 01 88",
    "abs": "61 80 39 36 30 35 52 08 30 33 37 39 33 35 31 52 45 69 01 70 00 37 02 01 00 88",
    "cluster": "61 80 33 35 34 34 52 13 39 32 4D 33 32 33 33 52 00 34 07 03 01 00 02 01 00 88",
    "body": "61 80 30 34 34 37 52 05 30 30 31 30 30 30 30 30 14 19 10 30 10 30 02 01 00 88",
    "airbag": "61 80 33 38 36 38 52 04 41 4D 52 30 32 30 34 30 16 30 16 30 00 A1 01 00 00 88",
}


class Replay:
    """Serial stand-in that answers each request with what the real car sent."""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.sent: list[str] = []
        self._out = bytearray()

    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def write(self, data: bytes) -> int:
        cmd = data.decode("ascii").strip()
        self.sent.append(cmd)
        reply = "OK" if cmd.startswith("AT") else self.answers.get(cmd, "NO DATA")
        self._out += (reply + "\r\r>").encode("ascii")
        return len(data)

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self._out[:size])
        del self._out[:size]
        return chunk

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        pass


def read_module(kind: str, tx: int, answer_1902: str) -> dict:
    replay = Replay({"1902AF": answer_1902})  # as if the module ignored the fault-bit mask
    elm = ELM327(replay)
    elm.protocol_num, elm.headers_on = "6", True
    res = EcuResult(kind, msg(f"ecu.{kind}"), f"{tx:03X}", f"{tx + 0x20:03X}", responded=True, protocol="KWP2000")
    EcuScanner(elm).read_details(res)
    return res.to_dict()


def codes(ecu: dict) -> list[tuple[str, str, str]]:
    return [(d["code"], d["raw"][4:], d["status_byte"]) for d in ecu["dtcs"]]


def test_abs_lists_28_tests_but_has_6_stored_codes():
    abs_ = read_module("abs", 0x740, ABS_1902)
    assert abs_["dtc_read_ok"] and abs_["notes"] == []
    assert codes(abs_) == [("C0020", "1C", "68"), ("U0422", "00", "28"), ("U0401", "00", "28"),
                           ("U0100", "00", "28"), ("U3003", "16", "28"), ("U3003", "13", "28")]
    assert all(d["state_id"] == "stored" for d in abs_["dtcs"])
    assert text(abs_["dtcs"][0]["description"]) == "ABS pump motor control"
    assert text(abs_["dtcs"][4]["description"], "es") == "Voltaje de la batería"


def test_cluster_has_4_codes_and_body_none():
    cluster = read_module("cluster", 0x743, CLUSTER_1902)
    assert codes(cluster) == [("U0001", "87", "28"), ("B1404", "13", "20"),
                              ("U3003", "16", "28"), ("B1412", "11", "20")]
    assert [d["state_id"] for d in cluster["dtcs"]] == ["stored", "failed_earlier", "stored", "failed_earlier"]
    body = read_module("body", 0x745, BODY_1902)
    assert body["dtc_read_ok"] and body["dtcs"] == []


def test_renault_identification_fields():
    abs_ = parse_renault_ident(bytes.fromhex(IDENTS["abs"]))
    assert abs_ == {"part_suffix": "9605R", "supplier": "037", "hw_suffix": "9351R",
                    "software": "4569", "edition": "0170"}
    assert parse_renault_ident(bytes.fromhex(IDENTS["airbag"]))["supplier"] == "AMR"
    assert parse_renault_ident(bytes.fromhex(IDENTS["cluster"]))["part_suffix"] == "3544R"
    for raw in IDENTS.values():
        assert set(parse_renault_ident(bytes.fromhex(raw))) == {"part_suffix", "supplier", "hw_suffix",
                                                                "software", "edition"}
    # anything that does not follow the layout falls back to the readable text
    assert parse_renault_ident(bytes.fromhex("6180") + b"\x01\x02ABCDEFG") == {"kwp_ident": "ABCDEFG"}


def test_findings_for_the_real_codes():
    ecus = [read_module("abs", 0x740, ABS_1902), read_module("cluster", 0x743, CLUSTER_1902),
            read_module("body", 0x745, BODY_1902)]
    report = {"ecus": ecus, "adapter": {"battery_voltage_start": 12.4},
              "obd": {"dtcs": {"stored": [], "pending": [], "permanent": []}, "live_data": []}}
    findings = rules.evaluate(report)
    report["findings"] = findings

    network = [f for f in findings if text(f["title"]).startswith("Communication / supply-voltage")]
    assert len(network) == 1
    assert network[0]["codes"] == ["U0001", "U0100", "U0401", "U0422", "U3003"]
    assert "U3003 (ABS / ESP brakes, Instrument cluster)" in text(network[0]["detail"])
    # the U codes are not repeated one by one
    assert not any(f is not network[0] and any(c.startswith("U") for c in f["codes"]) for f in findings)

    by_code = {f["codes"][0]: f["severity"] for f in findings if len(f["codes"]) == 1}
    assert by_code == {"C0020": "medium", "B1404": "low", "B1412": "low"}
    assert any(text(f["title"]).startswith("Battery slightly low: 12.4 V") for f in findings)

    summary = rules.summarize(report)
    assert summary["total_fault_codes"] == 10
    assert summary["status"] == "attention"
