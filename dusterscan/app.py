"""Desktop app: a native window (Edge WebView2 via pywebview) around the scanner."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import webview

from . import __version__, ai, i18n, rules, settings
from .battery import BatteryTest
from .ddt import match, store
from .ddt.reader import STANDARD_BUDGET_S, ModuleReader, enrich_module, groups_of, strip
from .elm327 import ELM327, AdapterError, list_ports, open_adapter
from .live import LiveMonitor
from .report_html import render_body, render_report
from .safety import WriteBlockedError
from .scanner import ScanCancelled, Scanner, odometer_from_modules
from .simulator import SimulatedCar


def _resource_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "dusterscan" / "ui"


class Api:
    """Methods callable from JavaScript as window.pywebview.api.<name>()."""

    def __init__(self):
        self._window: webview.Window | None = None
        self._elm: ELM327 | None = None
        self._report: dict | None = None
        self._ai: dict | None = None
        self._cancel = threading.Event()
        self._busy = False
        self._scanning = False
        self._lock = threading.Lock()
        # Live data runs on its own thread; it and the scan never use the adapter at the same time.
        self._live_stop = threading.Event()
        self._live_thread: threading.Thread | None = None
        self._demo = False           # connected to the simulated car (uses the demo database)
        self._report_live = False    # the open report came from this connection (modules can be re-read)
        self._importing = False
        self._battery_stop = threading.Event()
        self._battery_next = threading.Event()
        self._battery_result: dict | None = None   # last test this connection; joins the next scan

    # -- helpers -------------------------------------------------------------

    def _lang(self) -> str:
        return settings.load()["language"]

    def _t(self, key: str, **params) -> str:
        return i18n.t(key, self._lang(), **params)

    def _error_text(self, e: Exception) -> str:
        return e.text(self._lang()) if isinstance(e, i18n.UserError) else str(e)

    def _emit(self, event: str, payload) -> None:
        if self._window:
            self._window.evaluate_js(f"window.DS && DS.on({json.dumps(event)}, {json.dumps(payload)})")

    def _start(self, target, *args) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        def run():
            try:
                target(*args)
            finally:
                self._busy = False
        threading.Thread(target=run, daemon=True).start()
        return True

    # -- state, settings, language ------------------------------------------

    def get_state(self) -> dict:
        s = settings.load()
        return {
            "version": __version__,
            "language": s["language"],
            "languages": i18n.LANGUAGES,
            "strings": self.get_strings(s["language"]),
            "has_api_key": bool(s["api_key"] or os.environ.get("ANTHROPIC_API_KEY")),
            "connected": self._elm is not None,
            "has_report": self._report is not None,
            "reports_dir": str(settings.REPORTS_DIR),
            "ddt": self.ddt_status(),
            "odometer_km": s["odometer_km"],
            "odometer_at": s["odometer_at"],
        }

    def get_strings(self, lang: str) -> dict:
        """The UI strings ("ui.*") for a language."""
        return {k: v for k, v in i18n.strings(lang).items() if k.startswith("ui.")}

    def set_language(self, lang: str) -> dict:
        settings.save({"language": i18n.normalize(lang)})
        return self.get_state()

    def save_settings(self, api_key: str | None = None, odometer: str | None = None) -> dict:
        """api_key None = unchanged; odometer None = unchanged, "" = forget it."""
        if api_key is not None:
            settings.save({"api_key": api_key.strip()})
        if odometer is not None:
            digits = "".join(c for c in str(odometer) if c.isdigit())
            km = int(digits) if digits and 0 < int(digits) < 2_000_000 else None
            today = time.strftime("%Y-%m-%d") if km else None
            settings.save({"odometer_km": km, "odometer_at": today})
        return self.get_state()

    # -- adapter ---------------------------------------------------------------

    def list_ports(self) -> list[dict]:
        try:
            return list_ports()
        except Exception:
            return []

    def connect(self, port: str, demo: bool) -> dict:
        if self._scanning:
            return {"ok": False, "error": self._t("error.busy")}
        self.disconnect()
        try:
            if demo:
                elm = ELM327(SimulatedCar(latency=0.02), "Demo", 0)
            else:
                elm = open_adapter(port or None)
            elm.initialize()
        except (AdapterError, WriteBlockedError, OSError) as e:
            return {"ok": False, "error": self._error_text(e)}
        self._elm, self._demo, self._battery_result = elm, demo, None
        return {"ok": True, "port": elm.port_name, "baud": elm.baud, "version": elm.version,
                "protocol": elm.protocol_name, "voltage": elm.battery_voltage()}

    def disconnect(self) -> None:
        self.stop_live(wait=True)
        if self._elm:
            try:
                self._elm.close()
            except Exception:
                pass
        self._elm = None

    # -- scanning -------------------------------------------------------------

    def start_scan(self, deep: bool) -> dict:
        if not self._elm:
            return {"ok": False, "error": self._t("error.connect_first")}
        if self._busy:
            return {"ok": False, "error": self._t("error.busy")}
        self._scanning = True       # from here on live data refuses to start
        self.stop_live(wait=True)   # the scan needs the adapter
        self._cancel.clear()
        if not self._start(self._scan, deep):
            self._scanning = False
            return {"ok": False, "error": self._t("error.busy")}
        return {"ok": True}

    def cancel(self) -> None:
        self._cancel.set()

    def _scan(self, deep: bool) -> None:
        def progress(frac, message):
            self._emit("progress", {"fraction": frac, "message": message})
        s = settings.load()
        try:
            report = Scanner(self._elm, progress, self._cancel, lang=self._lang(), ddt=self._db(),
                             ddt_choices=self._choices(), odometer_km=s["odometer_km"]).run(deep=deep)
        except ScanCancelled:
            self._emit("scan_failed", {"error": self._t("error.scan_cancelled")})
            return
        except (AdapterError, WriteBlockedError, OSError) as e:
            self._emit("scan_failed", {"error": self._t("error.scan_stopped", error=self._error_text(e))})
            return
        except Exception as e:
            traceback.print_exc()
            self._emit("scan_failed", {"error": self._t("error.unexpected", error=str(e))})
            return
        finally:
            self._scanning = False
        if self._battery_result:  # a battery test made before this scan, on this connection
            report["battery_test"] = self._battery_result
            report["findings"] = rules.evaluate(report)
            report["summary"] = rules.summarize(report)
        self._report, self._ai, self._report_live = report, None, True
        saved = self._autosave()
        self._emit("scan_done", {"summary": report["summary"], "saved": saved})

    # -- Renault module database -------------------------------------------------

    def _db(self):
        """The database for the current car: demo definitions for the simulated car, else the imported one."""
        if self._demo or ((self._report or {}).get("ddt") or {}).get("source", {}).get("demo"):
            return store.demo()
        return store.load()

    def _choices(self) -> dict:
        """The user's definition picks for this car. The simulated car copies a real Duster's
        identifications, so its picks are kept apart ("demo|" keys) and never touch the real ones."""
        allc = settings.load()["ddt_choices"]
        if self._db() is store.demo():
            return {k[5:]: v for k, v in allc.items() if k.startswith("demo|")}
        return {k: v for k, v in allc.items() if not k.startswith("demo|")}

    def _save_choice(self, key: str, file: str) -> None:
        allc = dict(settings.load()["ddt_choices"])
        allc[("demo|" if self._db() is store.demo() else "") + key] = file
        settings.save({"ddt_choices": allc})

    def ddt_status(self) -> dict:
        s = store.status()
        return {"imported": bool(s), **(s or {}), "demo": self._demo, "importing": self._importing}

    def ddt_import(self) -> dict:
        if not self._window or self._importing:
            return {"ok": False}
        paths = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=(self._t("ui.file_zip"),))
        if not paths:
            return {"ok": False}
        self._importing = True
        threading.Thread(target=self._ddt_import, args=(paths[0],), daemon=True).start()
        return {"ok": True}

    def _ddt_import(self, path: str) -> None:
        try:
            store.import_zip(path, lambda f: self._emit("ddt_progress", {"fraction": round(f, 3)}))
        except (store.DatabaseError, OSError) as e:
            self._importing = False
            self._emit("ddt_failed", {"error": self._error_text(e)})
            return
        except Exception as e:
            traceback.print_exc()
            self._importing = False
            self._emit("ddt_failed", {"error": self._t("error.unexpected", error=str(e))})
            return
        self._importing = False
        self._apply_offline()
        self._emit("ddt_done", {"status": self.ddt_status()})

    def ddt_remove(self) -> dict:
        store.remove()
        return self.ddt_status()

    def _apply_offline(self) -> None:
        """Code names for the open report's modules from a newly imported database (no car needed)."""
        db = self._db()
        if not (db and self._report):
            return
        choices = self._choices()
        for ecu in self._report.get("ecus", []):
            choice = match.resolve(db, ecu, choices) if ecu.get("responded") else None
            if choice and not ecu.get("definition"):
                enrich_module(None, ecu, db, choice)
        self._refresh_report()

    def _refresh_report(self) -> None:
        r = self._report
        meta = r.setdefault("meta", {})
        km, source = odometer_from_modules(r.get("ecus", []))
        if km:  # the car's own reading wins over a typed one (see Scanner._set_odometer)
            meta["odometer_km"], meta["odometer_source"] = km, source
        r["findings"] = rules.evaluate(r)
        r["summary"] = rules.summarize(r)
        self._autosave()

    def _find_ecu(self, tx: str) -> dict | None:
        return next((e for e in (self._report or {}).get("ecus", []) if e.get("tx") == tx and e.get("responded")),
                    None)

    def ddt_modules(self) -> dict:
        """Rows of the "Module definitions" card for the open report."""
        db = self._db()
        if not (db and self._report and self._report.get("ecus")):
            return {"available": False, "rows": []}
        lang = self._lang()
        rows = []
        for ecu in self._report["ecus"]:
            if not ecu.get("responded"):
                continue
            d = ecu.get("definition") or {}
            rows.append({"tx": ecu["tx"], "name": i18n.text(ecu["name"], lang), "file": d.get("file"),
                         "ecuname": d.get("ecuname"), "tier": d.get("tier"), "chosen": d.get("chosen"),
                         "status": d.get("status"), "has_values": bool(ecu.get("module_data"))})
        return {"available": True, "rows": rows, "can_read": bool(self._elm and self._report_live),
                "used": sum(r["status"] == "used" for r in rows)}

    def ddt_candidates(self, tx: str, query: str = "", all_vehicles: bool = False) -> dict:
        db, ecu = self._db(), self._find_ecu(tx)
        if not (db and ecu):
            return {"items": [], "more": 0}
        project = ((self._report.get("ddt") or {}).get("project")
                   or match.vehicle_project(db, self._report.get("ecus", [])))
        return match.candidates(db, ecu, query or "", bool(all_vehicles), project)

    def ddt_choose(self, tx: str, file: str) -> dict:
        """The user picked a definition (or "generic") for one module."""
        db, ecu = self._db(), self._find_ecu(tx)
        if not (db and ecu) or (file != "generic" and file not in db.entries):
            return {"ok": False}
        if self._scanning or self._busy:
            return {"ok": False, "error": self._t("error.busy")}
        self._save_choice(match.module_key(ecu), file)
        if file == "generic":
            strip(ecu)
            self._refresh_report()
            self._emit("module_updated", {"tx": tx})
            return {"ok": True, "reading": False}
        choice = {"file": file, "tier": match.tier(db.entries[file], match.module_ident(ecu)), "chosen": "user"}
        if not (self._elm and self._report_live):
            enrich_module(None, ecu, db, choice)
            self._refresh_report()
            self._emit("module_updated", {"tx": tx})
            return {"ok": True, "reading": False}
        self._scanning = True
        self.stop_live(wait=True)
        if not self._start(self._reread_module, ecu, db, choice):
            self._scanning = False
            return {"ok": False, "error": self._t("error.busy")}
        return {"ok": True, "reading": True}

    def _reread_module(self, ecu: dict, db, choice: dict) -> None:
        deep = self._report.get("meta", {}).get("scan_type") == "deep"
        error = None
        try:
            self._elm.set_timeout_ms(1000)
            enrich_module(self._elm, ecu, db, choice, None if deep else STANDARD_BUDGET_S)
        except (AdapterError, WriteBlockedError, OSError) as e:
            error = self._error_text(e)
        finally:
            try:
                self._elm.set_timeout_ms(200)
                self._elm.reset_target()
            except (AdapterError, OSError):
                pass
            self._scanning = False
        self._refresh_report()
        self._emit("module_updated", {"tx": ecu["tx"], "error": error})

    # -- live data ---------------------------------------------------------------

    def live_sources(self) -> list[dict]:
        """What live data can show: the engine, and each module read with a Renault definition."""
        out = [{"id": "engine", "label": self._t("ui.live_engine"), "groups": []}]
        db = self._db()
        if db and self._report and self._report_live:
            for ecu in self._report.get("ecus", []):
                d = ecu.get("definition") or {}
                if d.get("status") == "used":
                    groups = groups_of(db, d)
                    if groups:
                        out.append({"id": ecu["tx"], "label": i18n.text(ecu["name"], self._lang()),
                                    "groups": groups})
        return out

    def start_live(self, source: str | None = None, group: str | None = None) -> dict:
        if not self._elm:
            return {"ok": False, "error": self._t("error.connect_first")}
        if self._scanning:
            return {"ok": False, "error": self._t("error.busy")}
        module = None
        if source and source != "engine":
            db, ecu = self._db(), self._find_ecu(source)
            d = (ecu or {}).get("definition") or {}
            if not (db and ecu and d.get("status") == "used"):
                return {"ok": False, "error": self._t("error.busy")}
            module = ModuleReader(self._elm, ecu, db, d)
            group = group or next(iter(groups_of(db, d)), None)
        self.stop_live(wait=True)  # switching source: stop the running one first
        self._live_stop.clear()
        self._live_thread = threading.Thread(target=self._live, args=(module, group), daemon=True)
        self._live_thread.start()
        return {"ok": True, "source": source or "engine", "group": group}

    def stop_live(self, wait: bool = False) -> None:
        self._live_stop.set()
        thread = self._live_thread
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=10)

    def _live(self, module=None, group=None) -> None:
        error = None
        try:
            LiveMonitor(self._elm, self._emit, self._live_stop, lang=self._lang(), module=module, group=group).run()
        except (AdapterError, WriteBlockedError, OSError) as e:
            error = self._t("error.live_stopped", error=self._error_text(e))
        except Exception as e:
            traceback.print_exc()
            error = self._t("error.unexpected", error=str(e))
        self._emit("live_stopped", {"error": error})

    # -- battery test ------------------------------------------------------------

    def start_battery(self) -> dict:
        if not self._elm:
            return {"ok": False, "error": self._t("error.connect_first")}
        if self._scanning or self._busy:
            return {"ok": False, "error": self._t("error.busy")}
        self._scanning = True
        self.stop_live(wait=True)
        self._battery_stop.clear()
        self._battery_next.clear()
        if not self._start(self._battery):
            self._scanning = False
            return {"ok": False, "error": self._t("error.busy")}
        return {"ok": True}

    def battery_next(self) -> None:
        self._battery_next.set()

    def stop_battery(self) -> None:
        self._battery_stop.set()

    def _battery(self) -> None:
        car = self._elm.t if isinstance(self._elm.t, SimulatedCar) else None

        def sim_hook(event: str) -> None:  # demo: the simulated car plays the driver's part
            if car is None:
                return
            if event == "crank":
                threading.Timer(1.5, car.crank).start()
            car.loads_on = event == "load_on"

        if car is not None:
            car.engine_running = False
        error = result = None
        try:
            result = BatteryTest(self._elm, self._emit, self._battery_stop, self._battery_next,
                                 self._lang(), sim_hook).run()
        except (AdapterError, WriteBlockedError, OSError) as e:
            error = self._t("error.battery_stopped", error=self._error_text(e))
        except Exception as e:
            traceback.print_exc()
            error = self._t("error.unexpected", error=str(e))
        finally:
            self._scanning = False
        if result:
            self._battery_result = result
            if self._report and self._report_live:
                self._report["battery_test"] = result
                self._refresh_report()
        self._emit("battery_done", {"result": result, "error": error, "cancelled": not (result or error)})

    def _autosave(self) -> str:
        try:
            html = render_report(self._report, self._ai, self._lang())
            return str(settings.save_report_files(self._report, html, self._ai))
        except OSError:
            return ""

    def get_report_html(self) -> str:
        return render_body(self._report, self._ai, self._lang()) if self._report else ""

    def get_report_summary(self) -> dict | None:
        """What the header shows about the open report."""
        if not self._report:
            return None
        status = self._report.get("summary", {}).get("status", "good")
        return {"vehicle": self._report.get("meta", {}).get("vehicle_hint", ""),
                "status": status, "label": self._t(f"report.status.{status}")}

    # -- AI ---------------------------------------------------------------------

    def start_ai(self, symptoms: str) -> dict:
        if not self._report:
            return {"ok": False, "error": self._t("error.scan_first")}
        s = settings.load()
        if not (s["api_key"] or os.environ.get("ANTHROPIC_API_KEY")):
            return {"ok": False, "error": self._t("error.need_api_key")}
        if not self._start(self._analyze, s["api_key"], s["language"], symptoms or ""):
            return {"ok": False, "error": self._t("error.busy")}
        return {"ok": True}

    def _analyze(self, api_key: str, lang: str, symptoms: str) -> None:
        t0 = time.monotonic()
        try:
            result = ai.analyze(self._report, api_key or None, lang, symptoms)
        except ai.AIError as e:
            self._emit("ai_failed", {"error": self._error_text(e)})
            return
        except Exception as e:
            traceback.print_exc()
            self._emit("ai_failed", {"error": self._t("error.unexpected", error=str(e))})
            return
        self._ai = result
        saved = self._autosave()
        self._emit("ai_done", {"seconds": round(time.monotonic() - t0), "saved": saved})

    # -- files ------------------------------------------------------------------

    def save_html(self) -> str:
        return self._save_dialog("html", lambda: render_report(self._report, self._ai, self._lang()))

    def save_json(self) -> str:
        return self._save_dialog("json", lambda: json.dumps(
            {"report": self._report, "ai_analysis": self._ai}, ensure_ascii=False, indent=1))

    def _save_dialog(self, ext: str, content) -> str:
        if not self._report or not self._window:
            return ""
        name = f"{settings.report_basename(self._report)}.{ext}"
        types = (self._t("ui.file_html") if ext == "html" else self._t("ui.file_json"),)
        path = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name, file_types=types)
        if not path:
            return ""
        path = path[0] if isinstance(path, (list, tuple)) else path
        Path(path).write_text(content(), encoding="utf-8")
        return str(path)

    def open_saved(self) -> dict:
        if not self._window:
            return {"ok": False}
        settings.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        paths = self._window.create_file_dialog(webview.FileDialog.OPEN, directory=str(settings.REPORTS_DIR),
                                                file_types=(self._t("ui.file_scan"),))
        if not paths:
            return {"ok": False}
        try:
            data = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
            self._report, self._ai, self._report_live = data["report"], data.get("ai_analysis"), False
        except (OSError, ValueError, KeyError):
            return {"ok": False, "error": self._t("error.not_a_scan")}
        return {"ok": True, "summary": self._report.get("summary", {})}

    def open_reports_folder(self) -> None:
        settings.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(settings.REPORTS_DIR)  # type: ignore[attr-defined]


def main() -> None:
    api = Api()
    window = webview.create_window(
        f"DusterScan {__version__}", url=str(_resource_dir() / "index.html"), js_api=api,
        width=1280, height=860, min_size=(820, 600), text_select=True,
        background_color="#09090b",  # app background, so there is no white flash before the page draws
    )
    api._window = window
    window.events.closed += api.disconnect
    webview.start(debug=bool(os.environ.get("DUSTERSCAN_DEBUG")))


if __name__ == "__main__":
    main()
