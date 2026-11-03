import json
import re
import threading
import time

from dusterscan.app import Api
from dusterscan.elm327 import ELM327
from dusterscan.live import LiveMonitor
from dusterscan.simulator import SimulatedCar


def connected_car():
    car = SimulatedCar()
    elm = ELM327(car, "SIM", 0)
    elm.initialize()
    car.received.clear()
    return car, elm


def run_live(updates: int):
    car, elm = connected_car()
    stop = threading.Event()
    events = []

    def emit(event, data):
        events.append((event, data))
        if sum(e == "live" for e, _ in events) >= updates:
            stop.set()

    LiveMonitor(elm, emit, stop, lang="es", min_cycle=0).run()
    return car, events


def test_live_reads_the_engine_continuously():
    car, events = run_live(updates=4)
    assert events[0][0] == "live_start"
    params = {p["pid"]: p for p in events[0][1]["params"]}
    assert params["0C"]["name"] == "Revoluciones del motor" and params["0C"]["unit"] == "rpm"
    assert "1C" not in params and "21" not in params   # counters that don't move are left out

    updates = [d for e, d in events if e == "live"]
    assert len(updates) == 4
    assert [u["t"] for u in updates] == sorted(u["t"] for u in updates)
    first = updates[0]["values"]
    assert "05" in first and "07" in first             # first refresh reads everything once
    assert first["04"] > 15                            # the engine's load, not the gearbox's 0 %
    assert first["03"] == "lazo cerrado"               # coded values arrive translated
    assert updates[0]["voltage"] == 14.1
    later = updates[1]["values"]
    assert "0C" in later and len(later) < len(first)   # later refreshes: fast values + a few others


def test_live_only_sends_reads():
    car, _ = run_live(updates=3)
    assert car.violations == []
    for cmd in car.received:
        assert cmd.startswith("AT") or re.fullmatch(r"01[0-9A-F]{2}", cmd), cmd


def test_starting_a_scan_stops_live_data():
    class Window:
        def __init__(self):
            self.events = []

        def evaluate_js(self, js):
            m = re.match(r"window\.DS && DS\.on\((\"\w+\"), (.*)\)$", js, re.S)
            self.events.append((json.loads(m.group(1)), json.loads(m.group(2))))

    def wait_for(name, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(e == name for e, _ in api._window.events):
                return True
            time.sleep(0.02)
        return False

    api = Api()
    api._window = Window()
    api._elm = connected_car()[1]
    api._lang = lambda: "en"
    api._autosave = lambda: ""   # don't write demo reports into the user's Documents
    assert api.start_live()["ok"]
    assert wait_for("live")
    assert api.start_scan(False) == {"ok": True}
    assert wait_for("scan_done", timeout=60)
    names = [e for e, _ in api._window.events]
    assert names.index("live_stopped") < names.index("progress")   # never both on the adapter
    assert names.count("live_stopped") == 1
    api.disconnect()
