"""Runs a complete read-only scan and builds the structured report."""

from __future__ import annotations

import datetime as dt
import statistics
import threading
import time
from typing import Callable

from . import __version__, obd2, rules
from . import dtc as dtcinfo
from .ddt import match
from .ddt.reader import STANDARD_BUDGET_S, enrich_module
from .ddt.store import Database
from .elm327 import ELM327, AdapterError, Message
from .i18n import msg, text
from .renault import KNOWN_ECUS, EcuScanner, sweep_addresses, valid_vin

ProgressFn = Callable[[float, str], None]

Target = tuple[int, int] | None   # (request ID, answer ID); None = OBD broadcast

ENGINE = (0x7E0, 0x7E8)
MODULE_DATA_BUDGET_S = 120.0   # all modules together, standard scan (a deep scan reads everything)


class ScanCancelled(Exception):
    pass


def odometer_from_modules(ecus: list[dict]) -> tuple[int | None, str | None]:
    """The total mileage a module reported through its Renault definition (cluster, body), if any."""
    for ecu in ecus:
        km = (ecu.get("module_data") or {}).get("odometer_km")
        if km:
            return int(km), ecu["kind"]
    return None, None


class Scanner:
    def __init__(self, elm: ELM327, progress: ProgressFn | None = None,
                 cancel: threading.Event | None = None, samples: int = 6, lang: str = "en",
                 ddt: Database | None = None, ddt_choices: dict | None = None,
                 odometer_km: int | None = None):
        self.elm = elm
        self.progress = progress or (lambda f, m: None)
        self.cancel = cancel or threading.Event()
        self.samples = samples
        self.lang = lang
        self.ddt = ddt                      # Renault module database, if imported
        self.ddt_choices = ddt_choices or {}
        self.odometer_km = odometer_km      # from Settings (optional); used if no module reports one
        self.messages: list[str] = []
        # Computers that answer standard OBD-II, engine first. Empty = use broadcast.
        self.obd_ecus: list[Target] = []
        self.engine: Target = None

    def _log(self, message) -> str:
        line = text(message, self.lang)
        stamp = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.messages.append(f"[{stamp}] {line}")
        return line

    def _step(self, frac: float, key: str, **params) -> None:
        if self.cancel.is_set():
            raise ScanCancelled()
        self.progress(frac, self._log(msg(key, **params)))

    # -- helpers -------------------------------------------------------------

    def _obd(self, payload: str, target: Target = None, timeout: float = 2.0) -> list[Message]:
        """OBD request to one computer (or broadcast); returns positive answers, engine (7E8) first."""
        try:
            if target:
                self.elm.set_target(*target)
            else:
                self.elm.reset_target()
            msgs = self.elm.request(payload, timeout=timeout)
        except AdapterError as e:
            self._log(msg("log.request_failed", request=payload, error=e.as_msg()))
            return []
        want = int(payload[:2], 16) + 0x40
        pos = [m for m in msgs if m.data and m.data[0] == want]
        pos.sort(key=lambda m: (m.header != "7E8", m.header or ""))
        return pos

    def _pid(self, pid: int, target: Target = None, retry: bool = False) -> bytes | None:
        for _ in range(2 if retry else 1):
            for m in self._obd(f"01{pid:02X}", target or self.engine):
                if len(m.data) >= 2 and m.data[1] == pid:
                    return m.data[2:]
        return None

    def discover_engine(self) -> set[int]:
        """Find the OBD-II computers and return the PIDs the engine computer supports."""
        self._find_obd_ecus()
        bitmaps: dict[int, bytes] = {}
        base = 0x00
        while base <= 0xA0:
            data = self._pid(base, retry=True)
            if base == 0x00 and not data and self.obd_ecus:
                # The engine did not answer when asked directly: fall back to broadcast.
                self.obd_ecus, self.engine = [], None
                data = self._pid(base)
            if not data or len(data) < 4:
                break
            bitmaps[base] = data[:4]
            if not data[3] & 0x01:  # bit for the next bitmap
                break
            base += 0x20
        return obd2.parse_supported(bitmaps)

    def read_pid(self, pid: int):
        """One decoded engine value (number or message), or None. Call discover_engine() first."""
        data = self._pid(pid)
        return obd2.decode_pid(pid, data) if data is not None else None

    def _find_obd_ecus(self) -> None:
        """Which computers answer OBD-II: the engine, and on automatics the gearbox too.

        Broadcast answers can't be trusted for data: on a real Duster the
        gearbox answers in a few ms, the adapter's adaptive timing then stops
        waiting before the slower engine computer answers, and the report
        showed the gearbox's values (0 % load, no fuel trims). So we only use
        the broadcast to find who is there, waiting long enough for everyone,
        and then ask each computer directly.
        """
        elm = self.elm
        if not (elm.is_can and elm.headers_on and not elm.is_29bit):
            return
        elm.reset_target()
        try:
            elm.command("ATAT0")
            elm.set_timeout_ms(400)
            msgs = elm.request("0100", timeout=3.0)
        except AdapterError as e:
            self._log(msg("log.request_failed", request="0100", error=e.as_msg()))
            msgs = []
        finally:
            elm.command("ATAT1")
            elm.set_timeout_ms(300)
        rxs = sorted({int(m.header, 16) for m in msgs
                      if m.header and len(m.header) == 3 and m.data[:1] == b"\x41"})
        found = [(rx - 8, rx) for rx in rxs if 0x7E8 <= rx <= 0x7EF]
        if ENGINE not in found:
            # Not heard on the broadcast: ask the engine directly before giving up on it.
            if any(len(m.data) >= 2 and m.data[1] == 0x00 for m in self._obd("0100", ENGINE)):
                found.insert(0, ENGINE)
        self.obd_ecus = sorted(found, key=lambda t: (t != ENGINE, t))
        self.engine = self.obd_ecus[0] if self.obd_ecus else None
        names = ", ".join(f"{rx:03X}" for _, rx in self.obd_ecus) or "-"
        self._log(msg("log.obd_ecus", ids=names))

    # -- the scan --------------------------------------------------------------

    def run(self, deep: bool = False) -> dict:
        started = time.monotonic()
        elm = self.elm
        report: dict = {
            "meta": {
                "app": "DusterScan", "app_version": __version__,
                "scanned_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "vehicle_hint": "Renault Duster 2018",
                "scan_type": "deep" if deep else "standard",
            },
        }

        self._step(0.02, "progress.adapter")
        voltage_start = elm.battery_voltage()
        report["adapter"] = {
            "port": elm.port_name, "baud": elm.baud, "version": elm.version,
            "protocol": elm.protocol_name, "battery_voltage_start": voltage_start,
        }

        # Supported PIDs
        self._step(0.05, "progress.supported")
        supported = self.discover_engine()

        # Monitor status / MIL. Readiness comes from the engine; the warning
        # light and code count add up over every OBD computer (the gearbox can
        # turn the light on too).
        self._step(0.10, "progress.mil")
        monitor = None
        others = []
        for target in self.obd_ecus or [None]:
            is_engine = target == self.engine
            if is_engine and 0x01 not in supported:
                continue
            data = self._pid(0x01, target, retry=is_engine)
            if data and len(data) >= 4:
                status = obd2.parse_monitor_status(data)
                if is_engine:
                    monitor = status
                else:
                    others.append(status)
        if monitor:
            for status in others:
                monitor["mil_on"] = monitor["mil_on"] or status["mil_on"]
                monitor["dtc_count"] += status["dtc_count"]

        # DTCs, from every OBD computer
        self._step(0.15, "progress.dtcs")
        obd_dtcs = {}
        dtc_read_from: set[str] = set()
        for mode, kind in (("03", "stored"), ("07", "pending"), ("0A", "permanent")):
            codes = []
            for target in self.obd_ecus or [None]:
                for m in self._obd(mode, target):
                    if mode == "03":
                        dtc_read_from.add(m.header or "engine")
                    for code in obd2.parse_obd_dtcs(m.data, elm.is_can):
                        desc, generic = dtcinfo.describe(code)
                        codes.append({"code": code, "description": desc, "generic": generic,
                                      "ecu": m.header or "engine"})
            obd_dtcs[kind] = codes

        # Freeze frame
        self._step(0.22, "progress.freeze")
        freeze = self._freeze_frame(supported)

        # Vehicle information
        self._step(0.27, "progress.vehicle")
        vehicle = self._vehicle_info()

        # Live data
        live = self._live_data(supported, 0.30, 0.55)

        # Mode 06
        mode06 = []
        if elm.is_can:
            self._step(0.56, "progress.mode06")
            mode06 = self._mode06()

        # Individual ECUs, then what the Renault database adds about them
        ecus = self._scan_ecus(deep, 0.60, 0.85 if self.ddt else 0.95)
        if self.ddt and elm.is_can:
            report["ddt"] = self._apply_database(ecus, deep, 0.85, 0.95)
        self._set_odometer(report["meta"], ecus)

        self._step(0.96, "progress.battery")
        elm.reset_target()
        report["adapter"]["battery_voltage_end"] = elm.battery_voltage()

        self._merge_module_data(vehicle, ecus, dtc_read_from)
        report["vehicle"] = vehicle
        report["obd"] = {
            "obd_ecus": [f"{rx:03X}" for _, rx in self.obd_ecus],
            "supported_pids": sorted(f"{p:02X}" for p in supported),
            "monitor_status": monitor,
            "dtcs": obd_dtcs,
            "dtc_read_from": sorted(dtc_read_from),
            "freeze_frame": freeze,
            "live_data": live,
            "mode06": mode06,
        }
        report["ecus"] = ecus
        report["meta"]["duration_s"] = round(time.monotonic() - started, 1)
        report["findings"] = rules.evaluate(report)
        report["summary"] = rules.summarize(report)
        self._step(1.0, "progress.done")
        report["scan_log"] = self.messages[-300:]
        # The whole raw adapter conversation (TX/RX), for troubleshooting a real car.
        report["adapter_log"] = [list(entry) for entry in elm.log]
        return report

    def _apply_database(self, ecus: list[dict], deep: bool, f0: float, f1: float) -> dict:
        """Renault definitions for the modules that match one: code names, code details, values."""
        db = self.ddt
        present = [e for e in ecus if e.get("responded")]
        started = time.monotonic()
        used = 0
        self.elm.set_timeout_ms(1000)  # the engine and airbag computers answer slowly
        try:
            for j, ecu in enumerate(present):
                choice = match.resolve(db, ecu, self.ddt_choices)
                if not choice:
                    continue
                self._step(f0 + (f1 - f0) * j / max(1, len(present)), "progress.module_data", name=ecu["name"])
                left = MODULE_DATA_BUDGET_S - (time.monotonic() - started)
                budget = None if deep else max(3.0, min(STANDARD_BUDGET_S, left))
                try:
                    enrich_module(self.elm, ecu, db, choice, budget, log=self._log)
                except AdapterError as e:
                    ecu.setdefault("notes", []).append(e.as_msg())
                used += ecu.get("definition", {}).get("status") == "used"
        finally:
            self.elm.set_timeout_ms(200)
            self.elm.reset_target()
        return {"source": db.info, "project": match.vehicle_project(db, present),
                "matched": used, "modules": len(present)}

    def _set_odometer(self, meta: dict, ecus: list[dict]) -> None:
        # A module's reading is today's mileage; the number in Settings may be weeks old.
        km, source = odometer_from_modules(ecus)
        if not km and self.odometer_km:
            km, source = int(self.odometer_km), "user"
        if km:
            meta["odometer_km"], meta["odometer_source"] = km, source

    def _merge_module_data(self, vehicle: dict, ecus: list[dict], dtc_read_from: set[str]) -> None:
        """Fill gaps in the OBD-II data with what the modules reported directly."""
        module_vins = {}
        for ecu in ecus:
            vin = valid_vin(ecu.get("identification", {}).get("vin"))
            if vin:
                module_vins[ecu["kind"]] = vin
            # The engine computer's own fault-code request may be refused while
            # the standard OBD-II read of the same computer worked.
            if ecu["tx"] == f"{ENGINE[0]:03X}" and not ecu.get("dtc_read_ok") and f"{ENGINE[1]:03X}" in dtc_read_from:
                ecu["dtc_via_obd"] = True
        if module_vins:
            vehicle["module_vins"] = module_vins
            if not valid_vin(vehicle.get("vin")):
                vehicle["vin"] = module_vins.get("engine") or next(iter(module_vins.values()))

    # -- sections ------------------------------------------------------------

    def _freeze_frame(self, supported: set[int]) -> dict | None:
        msgs = self._obd("020200", self.engine)
        trigger = None
        for m in msgs:
            if len(m.data) >= 5 and m.data[1] == 0x02:
                if m.data[3] or m.data[4]:
                    trigger = obd2.dtc_from_bytes(m.data[3], m.data[4])
                break
        if not trigger:
            return None
        values = []
        for pid in (0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x42):
            if pid not in supported:
                continue
            for m in self._obd(f"02{pid:02X}00", self.engine):
                if len(m.data) >= 3 and m.data[1] == pid:
                    v = obd2.decode_pid(pid, m.data[3:])
                    if v is not None:
                        p = obd2.PIDS[pid]
                        values.append({"pid": f"{pid:02X}", "name": p.name, "value": v, "unit": p.unit})
                    break
        return {"trigger_dtc": trigger, "trigger_description": dtcinfo.describe(trigger)[0],
                "values": values}

    def _vehicle_info(self) -> dict:
        info: dict = {}
        targets = self.obd_ecus or [None]

        def ask(payload: str) -> list[Message]:
            return [m for t in targets for m in self._obd(payload, t, timeout=3.0)]

        msgs = ask("0902")
        if msgs:
            if self.elm.is_can:
                info["vin"] = next((v for v in (obd2.parse_vin(m.data) for m in msgs) if valid_vin(v)),
                                   obd2.parse_vin(msgs[0].data))
            else:  # older protocols send the VIN as several numbered messages
                joined = b"".join(m.data[3:] for m in sorted(msgs, key=lambda m: m.data[2:3]))
                info["vin"] = obd2.parse_vin(b"\x49\x02\x01" + joined)
        for pid, key in (("04", "calibration_ids"), ("0A", "ecu_names")):
            items = []
            for m in ask("09" + pid):
                items += obd2.parse_text_items(m.data, 16 if pid == "04" else 20)
            if items:
                info[key] = items
        msgs = ask("0906")
        if msgs:
            info["calibration_verification_numbers"] = [m.data[3:].hex().upper() for m in msgs]
        return info

    def _live_data(self, supported: set[int], f0: float, f1: float) -> list[dict]:
        sampled = [p for p in obd2.SAMPLED_PIDS if p in supported]
        once = [p for p in obd2.PIDS if p in supported and p not in sampled]
        series: dict[int, list] = {p: [] for p in sampled}

        self._step(f0, "progress.once")
        single: dict[int, object] = {}
        for pid in once:
            data = self._pid(pid, retry=True)
            if data is not None:
                v = obd2.decode_pid(pid, data)
                if v is not None:
                    single[pid] = v

        for i in range(self.samples):
            self._step(f0 + (f1 - f0) * (i + 1) / (self.samples + 1),
                       "progress.sampling", i=i + 1, n=self.samples)
            for pid in sampled:
                data = self._pid(pid)
                if data is not None:
                    v = obd2.decode_pid(pid, data)
                    if v is not None:
                        series[pid].append(v)

        # Narrowband O2 sensors switch about once a second, so a few spread-out
        # samples say little. Read them back-to-back for a few seconds.
        o2 = [p for p in (0x14, 0x15) if p in supported]
        if o2:
            self._step(f1, "progress.o2")
            for _ in range(20):
                for pid in o2:
                    data = self._pid(pid)
                    v = obd2.decode_pid(pid, data) if data is not None else None
                    if v is not None:
                        series[pid].append(v)

        out = []
        for pid in sorted(set(series) | set(single)):
            p = obd2.PIDS[pid]
            row = {"pid": f"{pid:02X}", "name": p.name, "unit": p.unit,
                   "normal_range": list(p.normal) if p.normal else None, "note": p.note}
            vals = series.get(pid)
            if vals:
                nums = [v for v in vals if isinstance(v, (int, float))]
                row["samples"] = vals
                if nums:
                    row.update(value=nums[-1], min=min(nums), max=max(nums),
                               avg=round(statistics.fmean(nums), 3))
                else:
                    row["value"] = vals[-1]
            elif pid in single:
                row["value"] = single[pid]
            else:
                continue
            out.append(row)
        return out

    def _mode06(self) -> list[dict]:
        bitmaps = {}
        base = 0x00
        while base <= 0xE0:
            msgs = self._obd(f"06{base:02X}", self.engine)
            m = next((m for m in msgs if len(m.data) >= 6 and m.data[1] == base), None)
            if not m:
                break
            bitmaps[base] = m.data[2:6]
            if not m.data[5] & 0x01:
                break
            base += 0x20
        mids = sorted(x for x in obd2.parse_supported(bitmaps) if x % 0x20 != 0)
        results = []
        for mid in mids:
            for m in self._obd(f"06{mid:02X}", self.engine):
                if len(m.data) >= 2 and m.data[1] == mid:
                    results += obd2.parse_mode06(m.data)
                    break
        return results

    def _scan_ecus(self, deep: bool, f0: float, f1: float) -> list[dict]:
        if not self.elm.is_can:
            self._log(msg("log.not_can"))
            return []
        scanner = EcuScanner(self.elm, log=self._log)
        targets = list(KNOWN_ECUS) + (sweep_addresses() if deep else [])
        n_known, n_extra = len(KNOWN_ECUS), len(targets) - len(KNOWN_ECUS)
        found = []
        # Known modules get time to answer; the sweep of empty addresses has to
        # be quick. Reading fault codes gets the most: on a real Duster the
        # engine and airbag computers stay silent with a 150 ms timeout.
        self.elm.set_timeout_ms(500)
        try:
            for i, addr in enumerate(targets):
                frac = f0 + (f1 - f0) * 0.5 * i / len(targets)
                is_known = i < n_known
                if is_known:
                    self._step(frac, "progress.looking", name=addr.name)
                elif (i - n_known) % 8 == 0:
                    if i == n_known:
                        self.elm.set_timeout_ms(150)
                    self._step(frac, "progress.searching", i=i - n_known, n=n_extra)
                res = scanner.probe(addr, fast=not is_known)
                if res.responded or is_known:
                    found.append(res)
            present = [r for r in found if r.responded]
            self.elm.set_timeout_ms(1000)
            for j, res in enumerate(present):
                self._step(f0 + (f1 - f0) * (0.5 + 0.5 * j / max(1, len(present))),
                           "progress.reading_ecu", name=res.name)
                try:
                    scanner.read_details(res)
                except AdapterError as e:
                    res.notes.append(e.as_msg())
        finally:
            self.elm.set_timeout_ms(200)
            self.elm.reset_target()
        return [r.to_dict() for r in found]
