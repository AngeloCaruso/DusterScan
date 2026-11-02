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

from . import __version__, ai, i18n, settings
from .elm327 import ELM327, AdapterError, list_ports, open_adapter
from .live import LiveMonitor
from .report_html import render_body, render_report
from .safety import WriteBlockedError
from .scanner import ScanCancelled, Scanner
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
        }

    def get_strings(self, lang: str) -> dict:
        """The UI strings ("ui.*") for a language."""
        return {k: v for k, v in i18n.strings(lang).items() if k.startswith("ui.")}

    def set_language(self, lang: str) -> dict:
        settings.save({"language": i18n.normalize(lang)})
        return self.get_state()

    def save_settings(self, api_key: str | None) -> dict:
        if api_key is not None:
            settings.save({"api_key": api_key.strip()})
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
        self._elm = elm
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
        try:
            report = Scanner(self._elm, progress, self._cancel, lang=self._lang()).run(deep=deep)
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
        self._report, self._ai = report, None
        saved = self._autosave()
        self._emit("scan_done", {"summary": report["summary"], "saved": saved})

    # -- live data ---------------------------------------------------------------

    def start_live(self) -> dict:
        if not self._elm:
            return {"ok": False, "error": self._t("error.connect_first")}
        if self._scanning:
            return {"ok": False, "error": self._t("error.busy")}
        if self._live_thread and self._live_thread.is_alive():
            return {"ok": True}
        self._live_stop.clear()
        self._live_thread = threading.Thread(target=self._live, daemon=True)
        self._live_thread.start()
        return {"ok": True}

    def stop_live(self, wait: bool = False) -> None:
        self._live_stop.set()
        thread = self._live_thread
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=10)

    def _live(self) -> None:
        error = None
        try:
            LiveMonitor(self._elm, self._emit, self._live_stop, lang=self._lang()).run()
        except (AdapterError, WriteBlockedError, OSError) as e:
            error = self._t("error.live_stopped", error=self._error_text(e))
        except Exception as e:
            traceback.print_exc()
            error = self._t("error.unexpected", error=str(e))
        self._emit("live_stopped", {"error": error})

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
            self._report, self._ai = data["report"], data.get("ai_analysis")
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
