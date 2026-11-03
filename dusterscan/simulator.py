"""A simulated ELM327 adapter plugged into a simulated Renault Duster.

Used by the "Demo mode" in the app and by the tests, so everything can be
exercised without a car. It also records any request that is not a pure
read in ``violations`` - the tests assert that list stays empty.

It copies what a real 2018 Duster with automatic gearbox did on a vLinker FS:
  * the engine (7E8) and gearbox (7E9) both answer OBD-II broadcasts, and on
    a broadcast the slower engine's live-data answers get lost behind the
    gearbox's (asking the engine directly works);
  * modules identify with KWP2000 "21 80" but report fault codes with UDS
    "19 02", listing many tests that have not run yet next to the real codes;
  * the engine and airbag computers only answer "19 02" if the adapter waits
    at least 300 ms;
  * the gearbox answers "21 80" / "17" only in its extended session (10 C0).
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

WRITE_SERVICES = {0x04, 0x08, 0x11, 0x14, 0x27, 0x28, 0x2E, 0x2F, 0x30, 0x31,
                  0x34, 0x35, 0x36, 0x37, 0x3B, 0x3D, 0x85}

VIN = b"9FBHSRAD5JM000000"


def renault_ident(part: str, diag: int, supplier: str, hw: str, sw: str, edition: str) -> bytes:
    """A "61 80" answer in Renault's identification layout."""
    return (b"\x61\x80" + part.encode() + bytes([diag]) + supplier.encode() + hw.encode()
            + bytes.fromhex(sw) + bytes.fromhex(edition) + b"\x00\x01" + b"\x01\x01\x00\x88")


def uds_dtc(code: str, ftb: int, status: int) -> bytes:
    letter = "PCBU".index(code[0])
    hi = (letter << 6) | (int(code[1]) << 4) | int(code[2], 16)
    return bytes([hi, int(code[3:5], 16), ftb, status])


def _bitmap(pids: set[int], base: int) -> bytes:
    bits = sum(1 << (31 - (p - base - 1)) for p in pids if base < p <= base + 32)
    return bits.to_bytes(4, "big")


@dataclass
class SimEcu:
    rx: int
    ident: bytes | None                   # "61 80" answer; None = refuses 21 80 (UDS-only module)
    dtcs: list[bytes] | None = None       # "19 02" records; None = 19 not supported
    kwp_dtcs: bytes | None = None         # "57" answer; None = 17 not supported
    vin: bytes | None = VIN               # "21 81" / DID F190
    slow: bool = False                    # answers 19 only with a timeout >= 300 ms
    honours_mask: bool = True             # False = lists every record whatever status mask is asked
    session_only: bool = False            # 21 / 17 only in the extended session
    session: int = 0x81
    dids: dict = field(default_factory=dict)
    reads: dict = field(default_factory=dict)   # request hex -> reply, as in ddt/demo.py's definitions
    ext: dict = field(default_factory=dict)     # "19 06" records: DTC hex -> {record hex: data}


class SimulatedCar:
    """Implements the serial-port interface used by ELM327."""

    def __init__(self, engine_running: bool = True, seed: int | None = 7, latency: float = 0.0):
        self.rng = random.Random(seed)
        self.engine_running = engine_running
        self.latency = latency  # seconds per command, like a real adapter on the bus
        self._out = bytearray()
        self.echo = True
        self.headers = False
        self.header = 0x7DF
        self.cra = None
        self.timeout_ms = 200
        self.protocol = "0"
        self.violations: list[str] = []
        self.received: list[str] = []
        self.t0 = time.monotonic()
        self.ecus = self._build_ecus()
        # Battery test: a start-up voltage curve after crank(); brownout = the adapter resets in the dip
        self._crank_at: float | None = None
        self.loads_on = False
        self.brownout = False

    def crank(self) -> None:
        """The driver turns the key (battery test)."""
        self._crank_at = time.monotonic()

    def _voltage(self) -> float | None:
        if self._crank_at is not None:
            t = time.monotonic() - self._crank_at
            if t < 0.15:
                return 11.4
            if t < 1.2:
                if self.brownout:
                    self.engine_running = True
                    return None
                return 9.8 + (0.2 if int(t * 20) % 2 else 0.0)
            self.engine_running = True
            self._crank_at = None
        if not self.engine_running:
            return 12.4
        return 13.7 if self.loads_on else 14.1

    # -- serial interface --------------------------------------------------

    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def write(self, data: bytes) -> int:
        if self.latency:
            time.sleep(self.latency)
        for line in data.decode("ascii").split("\r"):
            if line.strip():
                self._handle(line.strip().upper().replace(" ", ""))
        return len(data)

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self._out[:size])
        del self._out[:size]
        return chunk

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        pass

    # -- the car -------------------------------------------------------------

    def _build_ecus(self) -> dict[int, SimEcu]:
        # Tests the module knows about but has not failed: status 0x50 / 0x10 / 0x40.
        def noise(*codes: str, status: int = 0x50) -> list[bytes]:
            return [uds_dtc(c, 0x00, status) for c in codes]

        def reads(**replies: str) -> dict:
            return {k[1:]: bytes.fromhex(v) for k, v in replies.items()}

        def ext(km: int, times: int) -> dict:
            return {"80": km.to_bytes(3, "big"), "82": bytes([times])}

        return {
            0x7E0: SimEcu(0x7E8, renault_ident("0960R", 0x44, "S3S", "4953R", "00DE", "4500"),
                          dtcs=[uds_dtc("P0171", 0x00, 0x28), uds_dtc("P0304", 0x00, 0x24)] + noise("P0300", "P0420"),
                          slow=True,
                          reads=reads(_222001="6220018D", _222002="62200282", _222003="622003031A",
                                      _222004="62200400", _222005="622005463452"),
                          ext={"017100": ext(89420, 3), "030400": ext(89950, 1)}),
            0x7E1: SimEcu(0x7E9, renault_ident("1146R", 0x07, "AIW", "2251R", "0042", "0102"),
                          kwp_dtcs=bytes([0x57, 0]), vin=None, session_only=True,
                          reads=reads(_2101="61017303", _2102="61022A")),
            0x740: SimEcu(0x760, renault_ident("9605R", 0x08, "037", "9351R", "4569", "0170"),
                          dtcs=[uds_dtc("C0020", 0x1C, 0x68), uds_dtc("U3003", 0x16, 0x28), uds_dtc("U0100", 0x00, 0x28)]
                          + noise("C0031", "C0034", "C0037", "C003A") + noise("U3003", status=0x40),
                          reads=reads(_224B01="624B010000", _224B02="624B0277"),
                          ext={"40201C": ext(85868, 2), "F00316": ext(85868, 5), "C10000": ext(85868, 1)}),
            0x743: SimEcu(0x763, None,
                          dtcs=[uds_dtc("U3003", 0x16, 0x28), uds_dtc("B1404", 0x13, 0x20)] + noise("B1412"),
                          dids={0xF187: b"248103544R", 0xF18A: b"92M"},
                          reads=reads(_220201="62020132")),
            0x745: SimEcu(0x765, renault_ident("0447R", 0x05, "001", "00000", "1419", "1030"),
                          dtcs=[uds_dtc("U0121", 0x87, 0x28)] + noise("B1504", "B1510", "B1550", status=0x10),
                          honours_mask=False,
                          reads=reads(_220101="62010186", _220102="62010200", _220103="620103015F90"),
                          ext={"C12187": ext(85000, 2)}),
            0x752: SimEcu(0x772, renault_ident("3868R", 0x04, "AMR", "02040", "1630", "1630"),
                          dtcs=[uds_dtc("B1047", 0x13, 0x28)], slow=True,
                          reads=reads(_2130="61302122"), ext={"904713": ext(61200, 1)}),
        }

    def _emit(self, text: str) -> None:
        self._out += (text + "\r\r>").encode("ascii")

    def _handle(self, cmd: str) -> None:
        self.received.append(cmd)
        if cmd.startswith("AT"):
            self._emit(self._at(cmd[2:]))
            return
        try:
            req = bytes.fromhex(cmd)
        except ValueError:
            self._emit("?")
            return
        if req[0] in WRITE_SERVICES or (req[0] == 0x10 and req[1:2] in (b"\x02", b"\x85")):
            self.violations.append(cmd)
            self._emit("NO DATA")
            return
        if self.protocol == "0" and req[:1] == b"\x01":
            self.protocol = "6"
            self._out += b"SEARCHING...\r"
        frames = self._respond(req)
        self._emit("\r".join(frames) if frames else "NO DATA")

    def _at(self, c: str) -> str:
        if c == "Z":
            self.__init__(self.engine_running, latency=self.latency)
            return "\r\rELM327 v1.5"
        if c == "I":
            return "ELM327 v1.5"
        if c == "RV":
            v = self._voltage()
            return "LV RESET" if v is None else f"{v:.1f}V"
        if c == "DPN":
            return "A" + self.protocol
        if c.startswith("E"):
            self.echo = c == "E1"
        elif c.startswith("H"):
            self.headers = c == "H1"
        elif c.startswith("SH"):
            self.header = int(c[2:], 16)
        elif c.startswith("CRA"):
            self.cra = int(c[3:], 16) if len(c) > 3 else None
        elif c.startswith("ST"):
            self.timeout_ms = int(c[2:], 16) * 4
        elif c.startswith("SP"):
            self.protocol = c[-1] if c[-1] != "0" else "0"
        return "OK"

    # -- responses -------------------------------------------------------------

    def _frames(self, rx: int, payload: bytes) -> list[str]:
        def fmt(b: bytes) -> str:
            body = " ".join(f"{x:02X}" for x in b)
            return f"{rx:03X} {body}" if self.headers else body
        if len(payload) <= 7:
            return [fmt(bytes([len(payload)]) + payload)]
        frames = [fmt(bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6])]
        rest, n = payload[6:], 1
        while rest:
            chunk = rest[:7]
            frames.append(fmt(bytes([0x20 | (n & 0xF)]) + chunk + b"\xAA" * (7 - len(chunk))))
            rest, n = rest[7:], n + 1
        return frames

    def _respond(self, req: bytes) -> list[str]:
        if req[0] <= 0x0A:
            return self._respond_obd(req)
        ecu = self.ecus.get(self.header)
        if not ecu or self.cra not in (None, ecu.rx):
            return []

        def answer(payload: bytes) -> list[str]:
            return self._frames(ecu.rx, payload)

        def refuse(nrc: int) -> list[str]:
            return answer(bytes([0x7F, req[0], nrc]))

        sid = req[0]
        locked = ecu.session_only and ecu.session != 0xC0
        if sid == 0x10:
            ecu.session = req[1]
            return answer(bytes([0x50, req[1]]))
        if sid == 0x3E:
            return answer(bytes([0x7E, req[1] if len(req) > 1 else 0]))
        if ecu.slow and self.timeout_ms < 300 and sid in (0x19, 0x21, 0x22) and req[1:2] != b"\x80":
            return []  # a slow module: the adapter gives up before it answers
        if req.hex().upper() in ecu.reads:
            return refuse(0x80) if locked else answer(ecu.reads[req.hex().upper()])
        if sid == 0x19 and len(req) == 6 and req[1] == 0x06 and ecu.dtcs is not None:
            record = next((r for r in ecu.dtcs if r[:3] == req[2:5]), None)
            data = ecu.ext.get(req[2:5].hex().upper(), {}).get(f"{req[5]:02X}")
            if record is None or data is None:
                return refuse(0x31)
            return answer(b"\x59\x06" + req[2:5] + record[3:4] + req[5:6] + data)
        if sid == 0x21:
            if ecu.ident is None:
                return refuse(0x11)
            if locked:
                return refuse(0x80)
            if req[1:2] == b"\x80":
                return answer(ecu.ident)
            if req[1:2] == b"\x81" and ecu.vin:
                return answer(b"\x61\x81" + ecu.vin + b"\x12\x34")
            return refuse(0x12)
        if sid == 0x22 and len(req) == 3:
            did = int.from_bytes(req[1:3], "big")
            if ecu.ident is not None:
                return refuse(0x11)
            value = VIN if did == 0xF190 else ecu.dids.get(did)
            return answer(b"\x62" + req[1:3] + value) if value else refuse(0x31)
        if sid == 0x19 and ecu.dtcs is not None:
            mask = req[2] if len(req) > 2 and ecu.honours_mask else 0xFF
            records = b"".join(r for r in ecu.dtcs if r[3] & mask)
            return answer(bytes([0x59, 0x02, 0xFF]) + records)
        if sid == 0x17 and ecu.kwp_dtcs is not None:
            return refuse(0x80) if locked else answer(ecu.kwp_dtcs)
        return refuse(0x11)

    def _respond_obd(self, req: bytes) -> list[str]:
        broadcast = self.header == 0x7DF
        out = []
        if broadcast or self.header == 0x7E0:
            data = self._engine_obd(req)
            # On a broadcast the engine's live-data answers come too late and get lost.
            if broadcast and req[0] == 0x01 and req[1] not in (0x00, 0x20, 0x40):
                data = None
            if data:
                out += self._frames(0x7E8, data)
        if broadcast or self.header == 0x7E1:
            data = self._gearbox_obd(req)
            if data:
                out += self._frames(0x7E9, data)
        if self.cra is not None:
            out = [f for f in out if not self.headers or f.startswith(f"{self.cra:03X}")]
        return out

    def _gearbox_obd(self, req: bytes) -> bytes | None:
        mode = req[0]
        if mode == 0x01:
            rpm = 760 if self.engine_running else 0
            table = {
                0x00: _bitmap({0x01, 0x04, 0x05, 0x0C, 0x0D, 0x1C, 0x20}, 0x00),
                0x20: _bitmap({0x21}, 0x20),
                0x01: bytes([0x00, 0x04, 0x00, 0x00]),   # MIL off, only "components" supported
                0x04: bytes([0]),
                0x05: bytes([90 + 40 if self.engine_running else 81 + 40]),
                0x0C: (rpm * 4).to_bytes(2, "big"),
                0x0D: bytes([0]),
                0x1C: bytes([6]),
                0x21: (0).to_bytes(2, "big"),
            }
            v = table.get(req[1])
            return bytes([0x41, req[1]]) + v if v is not None else None
        if mode in (0x03, 0x07, 0x0A):
            return bytes([mode + 0x40, 0x00])
        if mode == 0x09 and req[1] == 0x04:
            return b"\x49\x04\x01" + b"310321146R".ljust(16, b"\x00")
        return None

    def _engine_obd(self, req: bytes) -> bytes | None:
        mode = req[0]
        t = time.monotonic() - self.t0
        if mode == 0x01:
            pid = req[1]
            v = self._pid(pid, t)
            return bytes([0x41, pid]) + v if v is not None else None
        if mode == 0x02:
            pid = req[1]
            if pid == 0x02:
                return bytes([0x42, 0x02, 0x00, 0x01, 0x71])  # P0171 caused the freeze frame
            v = self._pid(pid, 0)
            return bytes([0x42, pid, 0x00]) + v if v is not None else None
        if mode == 0x03:
            return bytes([0x43, 0x01, 0x01, 0x71])        # P0171
        if mode == 0x07:
            return bytes([0x47, 0x01, 0x03, 0x04])        # P0304 pending
        if mode == 0x0A:
            return bytes([0x4A, 0x00])
        if mode == 0x09:
            if req[1] == 0x02:
                return b"\x49\x02\x01" + VIN
            if req[1] == 0x04:
                return b"\x49\x04\x01" + b"8201535235".ljust(16, b"\x00")
            return None
        if mode == 0x06:
            mid = req[1]
            if mid == 0x00:
                return bytes([0x46, 0x00, 0x00, 0x00, 0x00, 0x00])
            return None
        return None

    def _pid(self, pid: int, t: float) -> bytes | None:
        run = self.engine_running
        wobble = math.sin(t * 7.0)
        table = {
            # Supported-PID bitmaps: 01 03-07 0B-11 13-15 1C 1F | 21 30 31 33 | 42
            0x00: bytes([0xBE, 0x3F, 0xB8, 0x13]),
            0x20: bytes([0x80, 0x01, 0xA0, 0x01]),
            0x40: bytes([0x40, 0x00, 0x00, 0x00]),
            0x01: bytes([0x81, 0x07, 0x65, 0x21]),         # MIL on, 1 DTC, catalyst/O2 not complete
            0x03: bytes([0x02 if run else 0x00, 0x00]),
            0x04: bytes([int((24 + wobble) * 2.55)]) if run else bytes([0]),
            0x05: bytes([90 + 40 if run else 81 + 40]),
            0x06: bytes([128 + int(6 + 3 * wobble) * 128 // 100]) if run else bytes([128]),
            0x07: bytes([128 + 14 * 128 // 100]),            # +14% long term: lean
            0x0B: bytes([33 + int(2 * wobble)]) if run else bytes([75]),
            0x0C: (int((790 + 25 * wobble) * 4)).to_bytes(2, "big") if run else b"\x00\x00",
            0x0D: bytes([0]),
            0x0E: bytes([int((10 + 2 * wobble + 64) * 2)]),
            0x0F: bytes([32 + 40]),
            0x10: (int((3.1 + 0.2 * wobble) * 100)).to_bytes(2, "big") if run else b"\x00\x00",
            0x11: bytes([int(14 * 2.55)]),
            0x13: bytes([0x03]),
            0x14: bytes([int((0.45 + 0.38 * math.sin(t * 13)) * 200) if run else 90, 0xFF]),
            0x15: bytes([int(0.68 * 200) + int(4 * wobble), 0xFF]),
            0x1C: bytes([6]),
            0x1F: (1260 if run else 0).to_bytes(2, "big"),
            0x21: (37).to_bytes(2, "big"),
            0x30: bytes([42]),
            0x31: (1830).to_bytes(2, "big"),
            0x33: bytes([75]),
            0x42: (14100 if run else 12400).to_bytes(2, "big"),
        }
        return table.get(pid)
