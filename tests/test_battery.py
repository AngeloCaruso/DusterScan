import re
import threading

import pytest

from dusterscan import battery, rules
from dusterscan.battery import BatteryTest
from dusterscan.ddt import store
from dusterscan.ddt.reader import ModuleReader
from dusterscan.elm327 import ELM327
from dusterscan.i18n import text
from dusterscan.live import LiveMonitor
from dusterscan.renault import parse_renault_ident
from dusterscan.simulator import SimulatedCar


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """The real test waits seconds per step; here a fraction is enough."""
    for name in ("REST_S", "IDLE_S", "LOAD_S", "SETTLE_S"):
        monkeypatch.setattr(battery, name, 0.3)


def run_test(brownout=False, engine_running=False):
    car = SimulatedCar(engine_running=engine_running)
    elm = ELM327(car, "SIM", 0)
    elm.initialize()
    car.engine_running, car.brownout = engine_running, brownout
    car.received.clear()
    stop, advance = threading.Event(), threading.Event()
    events = []

    def emit(event, data):
        events.append((event, data))
        if event == "battery_step" and data.get("button"):
            if data["instruction"] == "ui.batt_instr_engine_off":
                car.engine_running = False   # the driver switches the engine off
            advance.set()                    # ... and presses the button

    def sim_hook(event):
        if event == "crank":
            threading.Timer(0.2, car.crank).start()
        car.loads_on = event == "load_on"

    result = BatteryTest(elm, emit, stop, advance, "en", sim_hook).run()
    return car, events, result


def test_guided_battery_test():
    car, events, result = run_test()
    assert car.violations == []
    assert result["rest_v"] == 12.4 and result["rest"] == "fair"
    assert 9.7 <= result["crank_min_v"] <= 10.0 and result["crank"] == "fair" and result["crank_detected"]
    assert result["crank_s"] and result["crank_s"] < 2
    assert result["idle_v"] == 14.1 and result["idle"] == "ok"
    assert result["load_v"] == 13.7 and result["load"] == "ok"
    steps = [d["step"] for e, d in events if e == "battery_step"]
    assert steps.index("rest") < steps.index("crank") < steps.index("idle") < steps.index("load")
    # only voltage readings and standard OBD-II reads
    for cmd in car.received:
        assert cmd.startswith("AT") or re.fullmatch(r"01[0-9A-F]{2}", cmd), cmd

    titles = [text(f["title"]) for f in rules.evaluate({"battery_test": result, "adapter": {}, "obd": {}})]
    assert titles[0].startswith("Battery borderline when starting: ")
    assert any(t.startswith("Battery not fully charged at rest: 12.4 V") for t in titles)


def test_engine_must_be_off_first():
    _, events, result = run_test(engine_running=True)
    assert any(d.get("instruction") == "ui.batt_instr_engine_off" for e, d in events if e == "battery_step")
    assert result["rest_v"] == 12.4


def test_adapter_brownout_means_a_weak_battery():
    car, _, result = run_test(brownout=True)
    assert result["brownout"] and result["crank"] == "weak"
    assert result["idle_v"] == 14.1   # the adapter was set up again and the test went on
    titles = [text(f["title"]) for f in rules.evaluate({"battery_test": result, "adapter": {}, "obd": {}})]
    assert titles[0] == "Battery too weak to start cleanly: the adapter lost power"
    assert car.violations == []


def test_cancel():
    car = SimulatedCar(engine_running=False)
    elm = ELM327(car, "SIM", 0)
    elm.initialize()
    stop, advance = threading.Event(), threading.Event()

    def emit(event, data):
        if event == "battery_step" and data.get("button"):
            stop.set()
    assert BatteryTest(elm, emit, stop, advance).run() is None


def test_live_module_values_use_the_extended_session_when_needed():
    car = SimulatedCar()
    elm = ELM327(car, "SIM", 0)
    elm.initialize()
    elm.set_timeout_ms(1000)
    elm.set_target(0x7E1, 0x7E9)
    elm.request("10C0")
    ident = next(m for m in elm.request("2180") if m.data[0] == 0x61).data
    elm.request("1081")
    gearbox = {"kind": "gearbox", "tx": "7E1", "rx": "7E9", "protocol": "KWP2000",
               "identification": parse_renault_ident(ident), "dtcs": []}
    reader = ModuleReader(elm, gearbox, store.demo(), {"file": "DEMO_DP0_Gearbox_v7.5_20161104T143902.json",
                                                       "tier": "exact", "chosen": "auto"})
    car.received.clear()
    stop, events = threading.Event(), []

    def emit(event, data):
        events.append((event, data))
        if sum(e == "live" for e, _ in events) >= 3:
            stop.set()
    LiveMonitor(elm, emit, stop, min_cycle=0, module=reader, group="Gearbox measures").run()
    assert [p["pid"] for p in events[0][1]["params"]] == ["Gearbox oil temperature", "Selected gear", "Line pressure"]
    values = [d["values"] for e, d in events if e == "live"]
    assert values[-1] == {"Gearbox oil temperature": 75, "Selected gear": "D", "Line pressure": 4.2}
    assert car.received.count("10C0") == 1 and car.received[-1] != "10C0" and "1081" in car.received
    for cmd in car.received:
        assert cmd.startswith("AT") or cmd in ("2101", "2102", "10C0", "1081"), cmd
    assert car.violations == []
