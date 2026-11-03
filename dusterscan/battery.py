"""Guided battery and charging test.

Four steps, with the driver's help: battery at rest (engine off), the voltage
dip while the engine starts, charging at idle, and charging with the big
electrical loads on. It sends only the adapter's own voltage reading (AT RV)
and two standard OBD-II reads from the engine (rpm 01 0C, module voltage 01 42).

The adapter measures at the OBD socket (pin 16), which typically reads
0.1-0.3 V below the battery terminals; the limits below are the usual ones for
a 12 V lead-acid battery.
"""

from __future__ import annotations

import datetime as dt
import statistics
import threading
import time
from typing import Callable

from .elm327 import ELM327, AdapterError
from .scanner import Scanner

REST_GOOD, REST_FAIR = 12.6, 12.4      # at rest: full charge / about 75 %
CRANK_GOOD, CRANK_FAIR = 10.0, 9.6     # lowest voltage while starting
CHARGE_MIN, CHARGE_MAX = 13.5, 14.8    # alternator at idle
LOAD_MIN = 13.2                         # alternator with lights, blower, defrost and A/C on

REST_S, IDLE_S, LOAD_S, SETTLE_S = 3.0, 8.0, 8.0, 3.0
CRANK_WAIT_S = 20.0
DIP_V, RECOVERED_V = 0.8, 0.6           # below rest = cranking; above rest = engine running
CRANK_TRIES = 3

EmitFn = Callable[[str, dict], None]


def grade_rest(v: float | None) -> str | None:
    if v is None:
        return None
    return "good" if v >= REST_GOOD else "fair" if v >= REST_FAIR else "low"


def grade_crank(min_v: float | None, brownout: bool) -> str | None:
    if brownout:
        return "weak"
    if min_v is None:
        return None
    return "good" if min_v >= CRANK_GOOD else "fair" if min_v >= CRANK_FAIR else "weak"


def grade_charge(v: float | None, low: float = CHARGE_MIN) -> str | None:
    if v is None:
        return None
    return "low" if v < low else "high" if v > CHARGE_MAX else "ok"


class TestCancelled(Exception):
    pass


class BatteryTest:
    def __init__(self, elm: ELM327, emit: EmitFn, stop: threading.Event, advance: threading.Event,
                 lang: str = "en", sim_hook: Callable[[str], None] = lambda event: None):
        self.elm = elm
        self.emit = emit
        self.stop = stop          # set by Cancel
        self.advance = advance    # set when the driver presses the step's button
        self.lang = lang
        self.sim_hook = sim_hook  # demo mode: plays the driver's part on the simulated car

    # -- helpers ---------------------------------------------------------------

    def _step(self, step: str, instruction: str, button: str | None = None) -> None:
        self.emit("battery_step", {"step": step, "instruction": instruction, "button": button})

    def _wait_user(self, step: str, instruction: str, button: str) -> None:
        self.advance.clear()
        self._step(step, instruction, button)
        while not self.advance.wait(0.1):
            if self.stop.is_set():
                raise TestCancelled()

    def _volt(self) -> float | None:
        try:
            return self.elm.battery_voltage()
        except AdapterError:
            return None

    def _sample(self, step: str, seconds: float) -> float | None:
        values = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop.is_set():
                raise TestCancelled()
            v = self._volt()
            if v:
                values.append(v)
                self.emit("battery_sample", {"step": step, "v": v})
            self.stop.wait(0.2)
        return round(statistics.median(values), 2) if values else None

    # -- the test --------------------------------------------------------------

    def run(self) -> dict | None:
        """The result, or None if cancelled."""
        try:
            return self._run()
        except TestCancelled:
            return None

    def _run(self) -> dict:
        link = Scanner(self.elm, lang=self.lang)
        supported = link.discover_engine()

        # 1. At rest, engine off
        while 0x0C in supported and (link.read_pid(0x0C) or 0) > 300:
            self._wait_user("rest", "ui.batt_instr_engine_off", "ui.batt_btn_retry")
        self._step("rest", "ui.batt_instr_rest")
        rest = self._sample("rest", REST_S)

        # 2. Starting the engine
        crank = None
        for attempt in range(CRANK_TRIES):
            self._wait_user("crank", "ui.batt_instr_crank_ready" if attempt == 0 else "ui.batt_instr_crank_missed",
                            "ui.batt_btn_ready")
            self._step("crank", "ui.batt_instr_crank_now")
            self.sim_hook("crank")
            crank = self._crank(rest or 12.6)
            if crank["detected"]:
                break
        if crank["brownout"]:
            try:  # the adapter restarted in the dip; set it up again for the next steps
                self.elm.initialize()
                link = Scanner(self.elm, lang=self.lang)
                supported = link.discover_engine()
            except AdapterError:
                pass

        # 3. Charging at idle
        self._step("idle", "ui.batt_instr_idle")
        self.stop.wait(SETTLE_S)
        idle = self._sample("idle", IDLE_S)
        ecu_v = link.read_pid(0x42) if 0x42 in supported else None

        # 4. Charging under load
        self._wait_user("load", "ui.batt_instr_load_ready", "ui.batt_btn_ready")
        self._step("load", "ui.batt_instr_load")
        self.sim_hook("load_on")
        self.stop.wait(SETTLE_S)
        load = self._sample("load", LOAD_S)
        self.sim_hook("done")

        result = {
            "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "rest_v": rest, "rest": grade_rest(rest),
            "crank_min_v": crank["min_v"], "crank_s": crank["dip_s"], "brownout": crank["brownout"],
            "crank_detected": crank["detected"], "crank": grade_crank(crank["min_v"], crank["brownout"]),
            "idle_v": idle, "idle": grade_charge(idle),
            "ecu_v": ecu_v if isinstance(ecu_v, (int, float)) else None,
            "load_v": load, "load": grade_charge(load, LOAD_MIN),
        }
        return result

    def _crank(self, rest: float) -> dict:
        """Sample as fast as the adapter allows while the driver starts the engine."""
        start = time.monotonic()
        min_v = dip_start = dip_end = None
        misses = 0
        brownout = False
        while time.monotonic() - start < CRANK_WAIT_S:
            if self.stop.is_set():
                raise TestCancelled()
            v = self._volt()
            if v is None:
                misses += 1
                if misses >= 2 and dip_start is None:
                    dip_start = time.monotonic()
                brownout = brownout or misses >= 2  # the adapter itself lost power in the dip
                continue
            misses = 0
            self.emit("battery_sample", {"step": "crank", "v": v})
            if v < rest - DIP_V:
                dip_start = dip_start or time.monotonic()
                min_v = v if min_v is None else min(min_v, v)
            elif dip_start is not None and v > rest + RECOVERED_V:
                dip_end = time.monotonic()
                break
        return {"min_v": min_v, "brownout": brownout, "detected": dip_start is not None,
                "dip_s": round(dip_end - dip_start, 2) if dip_start and dip_end else None}
