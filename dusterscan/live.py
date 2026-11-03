"""Continuous readings for the "Live data" view, until asked to stop.

Two sources:
  * the engine (default): standard OBD-II mode 01 requests to the engine
    computer, plus the adapter's own battery-voltage reading;
  * a module matched to a Renault definition: the read requests behind one of
    the definition's screens (see ddt/reader.py for what may be sent).
Both send only what a scan already sends.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from . import obd2
from .ddt.reader import ModuleReader
from .elm327 import ELM327
from .i18n import text
from .renault import extended_session
from .scanner import Scanner

# Read on every refresh: what changes from one second to the next.
FAST_PIDS = [0x0C, 0x0D, 0x04, 0x11, 0x0B, 0x10, 0x0E, 0x06, 0x14]
# Counters and identifiers that do not move while you watch.
STATIC_PIDS = {0x1C, 0x21, 0x30, 0x31}
SLOW_PER_CYCLE = 2      # the other supported values are read in turn, this many per refresh
MIN_CYCLE_S = 0.4       # refresh at most this often (a fast adapter or the demo would flood the window)
VOLTAGE_EVERY = 5       # refreshes between adapter battery-voltage readings

EmitFn = Callable[[str, dict], None]


class LiveMonitor:
    def __init__(self, elm: ELM327, emit: EmitFn, stop: threading.Event, lang: str = "en",
                 min_cycle: float = MIN_CYCLE_S, module: ModuleReader | None = None, group: str | None = None):
        self.elm = elm
        self.emit = emit
        self.stop = stop
        self.lang = lang
        self.min_cycle = min_cycle
        self.module = module   # None = the engine over OBD-II
        self.group = group

    def _describe(self, pid: int) -> dict:
        p = obd2.PIDS[pid]
        return {"pid": f"{pid:02X}", "name": text(p.name, self.lang), "unit": p.unit,
                "normal": list(p.normal) if p.normal else None}

    def run(self) -> None:
        if self.module is not None:
            self._run_module()
            return
        link = Scanner(self.elm, lang=self.lang)
        supported = link.discover_engine()
        fast = [p for p in FAST_PIDS if p in supported]
        slow = [p for p in obd2.PIDS if p in supported and p not in fast and p not in STATIC_PIDS]
        self.emit("live_start", {"params": [self._describe(p) for p in fast + slow]})
        if not fast and not slow:
            return

        t0 = time.monotonic()
        cycle = 0
        while not self.stop.is_set():
            started = time.monotonic()
            if cycle == 0 or not slow:
                batch = fast + slow
            else:
                first = (cycle - 1) * SLOW_PER_CYCLE
                batch = fast + list(dict.fromkeys(slow[(first + i) % len(slow)]
                                                  for i in range(SLOW_PER_CYCLE)))
            values = {}
            for pid in batch:
                if self.stop.is_set():
                    break
                v = link.read_pid(pid)
                if v is not None:
                    values[f"{pid:02X}"] = v if isinstance(v, (int, float)) else text(v, self.lang)
            update: dict = {"t": round(time.monotonic() - t0, 2), "values": values}
            if cycle % VOLTAGE_EVERY == 0:
                update["voltage"] = self.elm.battery_voltage()
            self.emit("live", update)
            cycle += 1
            self.stop.wait(max(0.0, self.min_cycle - (time.monotonic() - started)))

    def _run_module(self) -> None:
        reader = self.module
        requests = [] if reader.rejected else reader.group_requests(self.group or "")
        self.emit("live_start", {"params": reader.live_params(requests)})
        if not requests:
            return
        self.elm.set_timeout_ms(1000)  # some modules answer slowly
        session = None
        try:
            t0 = time.monotonic()
            while not self.stop.is_set():
                started = time.monotonic()
                values, refused = {}, False
                for r in requests:
                    if self.stop.is_set():
                        break
                    got, wrong_session = reader.read_once(r)
                    values.update(got)
                    refused |= wrong_session
                if refused and session is None:
                    # Some values are only given in the extended session: open it once for the
                    # whole live run (every request keeps it alive) and close it at the end.
                    session = extended_session(self.elm, *reader.session)
                    session.__enter__()
                    continue
                self.emit("live", {"t": round(time.monotonic() - t0, 2), "values": values})
                self.stop.wait(max(0.0, self.min_cycle - (time.monotonic() - started)))
        finally:
            if session is not None:
                session.__exit__(None, None, None)
            self.elm.set_timeout_ms(200)
            self.elm.reset_target()
