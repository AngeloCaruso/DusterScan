"""Updates from GitHub releases: checking, verified download, unpacking, the file swap and its rollback."""

import hashlib
import io
import re
import sys
import threading
import urllib.error
import zipfile
from pathlib import Path

import pytest

from dusterscan import __version__, app, updater
from dusterscan.i18n import UserError

ROOT = Path(__file__).resolve().parent.parent
ZIP_URL = "https://github.com/x/releases/download/v9.9.9/DusterScan-9.9.9-win64.zip"
SUMS_URL = "https://github.com/x/releases/download/v9.9.9/SHA256SUMS.txt"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(updater, "RETRY_S", 0)


class Response(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def serve(monkeypatch, files: dict):
    """Answers updater requests from `files` (url -> bytes); anything else is 'offline'."""
    def _open(url, accept, timeout=updater.TIMEOUT_S):
        if url not in files:
            raise urllib.error.URLError("offline")
        return Response(files[url])
    monkeypatch.setattr(updater, "_open", _open)


def release(version="9.9.9", assets=True, size=3, digest=None):
    files = [{"name": f"DusterScan-{version}-win64.zip", "size": size, "browser_download_url": ZIP_URL,
              "digest": digest},
             {"name": "SHA256SUMS.txt", "size": 90, "browser_download_url": SUMS_URL}]
    return {"tag_name": f"v{version}", "body": "- fix: something\n", "published_at": "2026-11-03T10:00:00Z",
            "html_url": "https://github.com/x/releases/tag/v" + version, "assets": files if assets else []}


def app_zip(path: Path, names=("DusterScan/DusterScan.exe", "DusterScan/DusterScan.exe.config",
                               "DusterScan/_internal/python.dll")) -> bytes:
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, f"new {n}")
    return path.read_bytes()


# -- versions and the release feed -----------------------------------------------

def test_versions_compare_as_numbers():
    assert updater.parse_version("v0.10.0") > updater.parse_version("0.9.9")
    assert updater.parse_version("1.2.3") == (1, 2, 3)
    assert updater.parse_version("v1.2") is None and updater.parse_version("1.2.3-beta") is None
    # the pipeline refuses other formats, and the updater relies on it
    assert updater.parse_version(__version__)


def test_release_is_read_and_compared(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.5.0")
    info = updater.parse_release(release("0.10.0"))
    assert info["newer"] and info["latest"] == "0.10.0" and info["current"] == "0.5.0"
    assert info["url"] == ZIP_URL and info["sums_url"] == SUMS_URL
    assert info["notes"] == "- fix: something" and info["published"] == "2026-11-03"
    assert not updater.parse_release(release("0.5.0"))["newer"]
    assert not updater.parse_release(release("0.4.9", assets=False))["newer"]   # older: files don't matter
    with pytest.raises(UserError) as e:
        updater.parse_release(release("0.6.0", assets=False))   # newer but published without its files
    assert e.value.key == "error.update_bad_release"
    with pytest.raises(UserError):
        updater.parse_release({"tag_name": "nightly"})


def test_check_handles_github_answers(monkeypatch):
    def fail(error):
        def _open(*a, **k):
            raise error
        monkeypatch.setattr(updater, "_open", _open)

    fail(urllib.error.HTTPError(updater.LATEST_URL, 404, "Not Found", {}, None))
    assert updater.check() == {"current": __version__, "latest": None, "newer": False}   # nothing released yet
    fail(urllib.error.HTTPError(updater.LATEST_URL, 403, "rate limited", {}, None))
    with pytest.raises(UserError) as e:
        updater.check()
    assert e.value.key == "error.update_github" and e.value.params == {"status": 403}
    fail(urllib.error.URLError("no route"))
    with pytest.raises(UserError) as e:
        updater.check()
    assert e.value.key == "error.update_offline"


def test_only_https_unless_the_feed_is_local(monkeypatch):
    assert updater._allowed("https://github.com/x") and not updater._allowed("http://github.com/x")
    assert not updater._allowed("http://127.0.0.1:8000/x.zip")
    monkeypatch.setattr(updater, "LATEST_URL", "http://127.0.0.1:8000/latest.json")
    assert updater._allowed("http://127.0.0.1:8000/x.zip")
    assert not updater._allowed("http://example.com/x.zip")


# -- download ----------------------------------------------------------------------

def test_download_is_verified(monkeypatch):
    data = b"the new app" * 1000
    good = hashlib.sha256(data).hexdigest()
    serve(monkeypatch, {ZIP_URL: data, SUMS_URL: f"{good}  DusterScan-9.9.9-win64.zip\n".encode()})
    info = updater.parse_release(release(size=len(data), digest=f"sha256:{good}"))
    seen = []
    path = updater.download(info, seen.append)
    assert path.read_bytes() == data and path.parent == updater.UPDATE_DIR
    assert seen[-1] == 1.0 and not list(updater.UPDATE_DIR.glob("*.part"))


@pytest.mark.parametrize("problem", ["checksum", "digest", "size", "no_line"])
def test_a_download_that_does_not_match_is_discarded(monkeypatch, problem):
    data = b"the new app"
    good = hashlib.sha256(data).hexdigest()
    sums = f"{good}  DusterScan-9.9.9-win64.zip\n"
    digest, size = f"sha256:{good}", len(data)
    if problem == "checksum":
        sums = f"{'0' * 64}  DusterScan-9.9.9-win64.zip\n"
    elif problem == "digest":
        digest = f"sha256:{'1' * 64}"
    elif problem == "size":
        size += 1
    else:
        sums = f"{good}  some-other-file.zip\n"
    serve(monkeypatch, {ZIP_URL: data, SUMS_URL: sums.encode()})
    with pytest.raises(UserError) as e:
        updater.download(updater.parse_release(release(size=size, digest=digest)))
    assert e.value.key in ("error.update_corrupt", "error.update_bad_release")
    assert not updater.UPDATE_DIR.exists() or not any(updater.UPDATE_DIR.iterdir())


def test_download_can_be_cancelled_and_offline_is_reported(monkeypatch):
    data = b"x" * (updater.CHUNK * 3)
    serve(monkeypatch, {ZIP_URL: data, SUMS_URL: f"{hashlib.sha256(data).hexdigest()}  DusterScan-9.9.9-win64.zip".encode()})
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(updater.UpdateCancelled):
        updater.download(updater.parse_release(release(size=len(data))), cancel=cancel)
    assert not any(updater.UPDATE_DIR.iterdir())
    serve(monkeypatch, {})
    with pytest.raises(UserError) as e:
        updater.download(updater.parse_release(release()))
    assert e.value.key == "error.update_offline"


# -- unpacking -----------------------------------------------------------------------

def test_stage_unpacks_the_app_folder(tmp_path):
    z = tmp_path / "r.zip"
    app_zip(z)
    folder = updater.stage(z)
    assert folder == updater.UPDATE_DIR / "staged" / "DusterScan"
    assert (folder / "DusterScan.exe").read_text() == "new DusterScan/DusterScan.exe"


@pytest.mark.parametrize("names", [
    ("DusterScan/DusterScan.exe", "DusterScan/_internal/a.dll", "../escaped.txt"),   # zip-slip
    ("DusterScan/DusterScan.exe",),                                                  # no _internal
    ("Other/Other.exe", "Other/_internal/a.dll"),                                    # not DusterScan
])
def test_stage_refuses_unexpected_zips(tmp_path, names):
    z = tmp_path / "r.zip"
    app_zip(z, names)
    with pytest.raises(UserError) as e:
        updater.stage(z)
    assert e.value.key == "error.update_corrupt"
    assert not (updater.UPDATE_DIR / "escaped.txt").exists() and not (tmp_path / "escaped.txt").exists()


def test_stage_refuses_a_file_that_is_not_a_zip(tmp_path):
    bad = tmp_path / "r.zip"
    bad.write_bytes(b"<html>not a zip</html>")
    with pytest.raises(UserError):
        updater.stage(bad)


# -- swapping the files ----------------------------------------------------------------

def make_app(folder: Path, label: str) -> Path:
    (folder / "_internal" / "sub").mkdir(parents=True)
    (folder / "DusterScan.exe").write_text(f"{label} exe")
    (folder / "DusterScan.exe.config").write_text(f"{label} config")
    (folder / "_internal" / "python.dll").write_text(f"{label} dll")
    (folder / "_internal" / "sub" / "x.pyd").write_text(f"{label} pyd")
    return folder


def contents(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): p.read_text() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_swap_replaces_only_the_app_files(tmp_path):
    install = make_app(tmp_path / "install", "old")
    (install / "my notes.txt").write_text("mine")
    staged = make_app(tmp_path / "staged", "new")
    updater.swap(staged, install)
    now = contents(install)
    assert now["DusterScan.exe"] == "new exe" and now["_internal/sub/x.pyd"] == "new pyd"
    assert now["my notes.txt"] == "mine"
    assert now[".previous/DusterScan.exe"] == "old exe"   # kept until the new version has started


def test_swap_failure_while_copying_restores_the_old_version(tmp_path, monkeypatch):
    install = make_app(tmp_path / "install", "old")
    (install / "my notes.txt").write_text("mine")
    before = contents(install)
    staged = make_app(tmp_path / "staged", "new")

    def broken_copytree(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(updater.shutil, "copytree", broken_copytree)
    with pytest.raises(OSError):
        updater.swap(staged, install)
    assert {k: v for k, v in contents(install).items() if not k.startswith(".previous/")} == before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking")
def test_swap_failure_while_moving_keeps_the_old_version(tmp_path):
    install = make_app(tmp_path / "install", "old")
    before = contents(install)
    staged = make_app(tmp_path / "staged", "new")
    # A file open inside _internal (antivirus, a stuck process) stops Windows from moving the folder.
    with open(install / "_internal" / "python.dll"):
        with pytest.raises(OSError):
            updater.swap(staged, install)
    assert {k: v for k, v in contents(install).items() if not k.startswith(".previous/")} == before


def test_apply_main_swaps_and_starts_the_right_version(tmp_path, monkeypatch):
    install = make_app(tmp_path / "install", "old")
    staged = make_app(tmp_path / "staged", "new")
    started = []
    monkeypatch.setattr(updater, "_start", lambda exe, *args: started.append((Path(exe), args)))
    monkeypatch.setattr(updater, "_wait_exit", lambda pid, timeout: True)
    monkeypatch.setattr(sys, "executable", str(staged / "DusterScan.exe"))
    args = ["--apply-update", str(install), "--pid", "1234", "--from", "0.5.0"]

    assert updater.apply_main(args) == 0
    assert started == [(install / "DusterScan.exe", ("--updated", "0.5.0"))]
    assert (install / "DusterScan.exe").read_text() == "new exe"
    assert "installed" in updater.LOG_FILE.read_text(encoding="utf-8")

    started.clear()
    monkeypatch.setattr(updater, "swap", lambda s, i: (_ for _ in ()).throw(OSError("locked")))
    assert updater.apply_main(args) == 1
    assert started == [(install / "DusterScan.exe", ("--update-failed",))]

    started.clear()
    monkeypatch.setattr(updater, "_wait_exit", lambda pid, timeout: False)   # the old app never closed
    assert updater.apply_main(args) == 3 and not started
    assert updater.apply_main(["--apply-update", str(tmp_path / "elsewhere"), "--pid", "1"]) == 2 and not started


def test_launch_starts_the_new_copy_with_the_contract_arguments(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **k: calls.append((cmd, k)))
    updater.launch(tmp_path / "staged", tmp_path / "install")
    cmd, kwargs = calls[0]
    assert cmd == [str(tmp_path / "staged" / "DusterScan.exe"), "--apply-update", str(tmp_path / "install"),
                   "--pid", str(__import__("os").getpid()), "--from", __version__]
    assert kwargs["cwd"] == str(tmp_path / "staged")   # never inside the folder being replaced


def test_cleanup_removes_leftovers(tmp_path, monkeypatch):
    install = make_app(tmp_path / "install", "new")
    make_app(install / ".previous", "old")
    (updater.UPDATE_DIR / "staged").mkdir(parents=True)
    monkeypatch.setattr(updater, "install_dir", lambda: install)
    updater.cleanup()
    assert not (install / ".previous").exists() and not updater.UPDATE_DIR.exists()
    assert (install / "DusterScan.exe").read_text() == "new exe"


# -- the app ---------------------------------------------------------------------------

class FakeWindow:
    def __init__(self):
        self.events, self.destroyed = [], False

    def evaluate_js(self, js):
        self.events.append(re.match(r'window\.DS && DS\.on\("(\w+)"', js).group(1))

    def destroy(self):
        self.destroyed = True


def test_from_source_the_app_checks_but_does_not_install():
    api = app.Api()
    assert api.get_state()["update"]["blocked"] == "source"
    api._update = {"newer": True, "latest": "9.9.9"}
    r = api.install_update()
    assert not r["ok"] and "git pull" in r["error"]


def test_install_is_refused_while_busy_or_without_an_update(monkeypatch):
    api = app.Api()
    monkeypatch.setattr(api, "_blocked", lambda: None)
    assert api.install_update()["error"] == api._t("error.update_none")
    api._update = {"newer": True, "latest": "9.9.9"}
    api._scanning = True
    assert api.install_update()["error"] == api._t("error.update_busy")


def test_install_downloads_launches_and_closes(tmp_path, monkeypatch):
    api = app.Api()
    api._window = FakeWindow()
    api._update = {"newer": True, "latest": "9.9.9"}
    monkeypatch.setattr(api, "_blocked", lambda: None)
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(updater, "download", lambda info, progress, cancel: (progress(1.0), tmp_path / "z.zip")[1])
    monkeypatch.setattr(updater, "stage", lambda z: calls.append(("stage", z)) or tmp_path / "staged")
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path / "install")
    monkeypatch.setattr(updater, "launch", lambda s, i: calls.append(("launch", s, i)))

    api._install_update(api._update)
    assert calls == [("stage", tmp_path / "z.zip"), ("launch", tmp_path / "staged", tmp_path / "install")]
    assert api._window.events == ["update_progress", "update_progress", "update_progress"] and api._window.destroyed

    def fails(*a):
        raise UserError("error.update_corrupt")
    monkeypatch.setattr(updater, "download", fails)
    api._window = FakeWindow()
    api._install_update(api._update)
    assert api._window.events == ["update_failed"] and not api._window.destroyed


def test_self_check_passes_from_source(tmp_path):
    report = tmp_path / "self-check.txt"
    assert app.self_check(["--self-check", str(report)]) == 0
    assert "FAIL" not in report.read_text(encoding="utf-8")


def test_pipeline_publishes_what_the_updater_looks_for():
    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert "DusterScan-$VERSION-win64.zip" in workflow and updater.asset_name("$VERSION") == "DusterScan-$VERSION-win64.zip"
    assert updater.CHECKSUMS in workflow and 'gh release create "v$VERSION"' in workflow
    assert updater.REPO == "AngeloCaruso/DusterScan"
