"""Updates from the project's GitHub releases.

The pipeline (.github/workflows/build.yml) publishes every version as a release with two
assets: DusterScan-<version>-win64.zip (the app folder) and SHA256SUMS.txt. Updating:

1. check()       asks GitHub for the latest release and compares its version with this one;
2. download()    fetches the zip into %LOCALAPPDATA%\\DusterScan\\update and checks its SHA-256;
3. stage()       unpacks it there;
4. launch()      starts the NEW version's DusterScan.exe from that copy, and this app closes
                 (Windows won't replace a running .exe or its DLLs, so another process must);
5. apply_main()  runs in the new version: it waits for the old app to exit, moves the old app
                 files into <install>\\.previous, copies the new ones in and starts the installed
                 new version with --updated. On any failure it puts the old files back and starts
                 the old version with --update-failed;
6. cleanup()     (next start) deletes .previous and the download.

Only the app's own files (APP_FILES) are moved or replaced; anything else in the folder stays.
Nothing here talks to the car.

The command line is a contract between versions, because version N starts version N+1's code:
    DusterScan.exe --apply-update <install folder> --pid <old process id> --from <old version>
and version N+1 then starts the installed app with `--updated <old version>` or `--update-failed`.
Keep it compatible.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import __version__
from .i18n import UserError

REPO = "AngeloCaruso/DusterScan"
# DUSTERSCAN_UPDATE_URL points the app at another release feed (a local test server).
LATEST_URL = os.environ.get("DUSTERSCAN_UPDATE_URL") or f"https://api.github.com/repos/{REPO}/releases/latest"
LOCAL_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "DusterScan"
UPDATE_DIR = Path(os.environ.get("DUSTERSCAN_UPDATE_DIR") or LOCAL_DIR / "update")
LOG_FILE = LOCAL_DIR / "update.log"   # outside UPDATE_DIR, so it survives cleanup()

APP_FILES = ("DusterScan.exe", "DusterScan.exe.config", "_internal")
PREVIOUS = ".previous"
CHECKSUMS = "SHA256SUMS.txt"
TIMEOUT_S = 15
CHUNK = 256 * 1024
WAIT_EXIT_S = 60       # for the old app to close
RETRIES, RETRY_S = 10, 1.0   # antivirus scanners briefly lock files that were just closed or written

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateCancelled(Exception):
    pass


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION.match((text or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def asset_name(version: str) -> str:
    return f"DusterScan-{version}-win64.zip"


# -- checking and downloading ---------------------------------------------------

def _allowed(url: str) -> bool:
    # HTTPS only, except when the feed itself was pointed at a local test server.
    local = LATEST_URL.startswith(("http://127.0.0.1", "http://localhost"))
    return url.startswith("https://") or (local and url.startswith(("http://127.0.0.1", "http://localhost")))


def _open(url: str, accept: str, timeout: float = TIMEOUT_S):
    if not _allowed(url or ""):
        raise UserError("error.update_bad_release")
    req = urllib.request.Request(url, headers={"User-Agent": f"DusterScan/{__version__}", "Accept": accept})
    return urllib.request.urlopen(req, timeout=timeout)


def check() -> dict:
    """The latest release: {current, latest, newer, notes, published, page, size, ...}."""
    try:
        with _open(LATEST_URL, "application/vnd.github+json") as r:
            release = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:   # no release published yet
            return {"current": __version__, "latest": None, "newer": False}
        raise UserError("error.update_github", status=e.code) from e
    except (OSError, ValueError) as e:
        raise UserError("error.update_offline") from e
    return parse_release(release)


def parse_release(release: dict) -> dict:
    latest = parse_version(release.get("tag_name", ""))
    if not latest:
        raise UserError("error.update_bad_release")
    version = "%d.%d.%d" % latest
    info = {
        "current": __version__, "latest": version,
        "newer": latest > (parse_version(__version__) or (0, 0, 0)),
        "notes": (release.get("body") or "").strip(),
        "published": (release.get("published_at") or "")[:10],
        "page": release.get("html_url"),
    }
    assets = {a.get("name"): a for a in release.get("assets") or []}
    zip_asset, sums = assets.get(asset_name(version)), assets.get(CHECKSUMS)
    if zip_asset and sums:
        info.update(size=zip_asset.get("size"), url=zip_asset.get("browser_download_url"),
                    sums_url=sums.get("browser_download_url"), digest=zip_asset.get("digest"))
    elif info["newer"]:
        raise UserError("error.update_bad_release")   # published without its files
    return info


def _expected_sha256(info: dict) -> str:
    try:
        with _open(info["sums_url"], "application/octet-stream") as r:
            sums = r.read(64 * 1024).decode("utf-8", "replace")
    except OSError as e:
        raise UserError("error.update_offline") from e
    name = asset_name(info["latest"])
    expected = None
    for line in sums.splitlines():   # sha256sum format: "<hex>  <name>" (or "<hex> *<name>")
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            expected = parts[0].lower()
    if not expected:
        raise UserError("error.update_bad_release")
    digest = info.get("digest") or ""   # GitHub's own record of the file, when it gives one
    if digest.startswith("sha256:") and digest[7:].lower() != expected:
        raise UserError("error.update_corrupt")
    return expected


def download(info: dict, progress=lambda fraction: None, cancel=None) -> Path:
    """The verified zip in UPDATE_DIR. Anything that does not match the checksum is deleted."""
    expected = _expected_sha256(info)
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPDATE_DIR / asset_name(info["latest"])
    part = dest.with_name(dest.name + ".part")
    sha, done, size = hashlib.sha256(), 0, info.get("size") or 0
    try:
        with _open(info["url"], "application/octet-stream") as r, open(part, "wb") as f:
            total = size or int(r.headers.get("Content-Length") or 0)
            while chunk := r.read(CHUNK):
                if cancel is not None and cancel.is_set():
                    raise UpdateCancelled
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if total:
                    progress(min(done / total, 1.0))
    except UpdateCancelled:
        part.unlink(missing_ok=True)
        raise
    except OSError as e:
        part.unlink(missing_ok=True)
        raise UserError("error.update_offline") from e
    if (size and done != size) or sha.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise UserError("error.update_corrupt")
    part.replace(dest)
    return dest


def stage(zip_path: Path) -> Path:
    """Unpacks the release; returns the new app folder (the one holding DusterScan.exe)."""
    target = UPDATE_DIR / "staged"
    _remove(target)
    root = target.resolve()
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = [n.replace("\\", "/") for n in z.namelist()]
            for n in names:   # nothing may land outside the folder ("../" or absolute paths)
                if not (root / n).resolve().is_relative_to(root):
                    raise UserError("error.update_corrupt")
            if "DusterScan/DusterScan.exe" not in names or not any(n.startswith("DusterScan/_internal/") for n in names):
                raise UserError("error.update_corrupt")
            z.extractall(target)
    except (zipfile.BadZipFile, OSError) as e:
        _remove(target)
        raise UserError("error.update_corrupt") from e
    return target / "DusterScan"


# -- installing -----------------------------------------------------------------

def install_dir() -> Path | None:
    """The folder DusterScan.exe runs from; None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def writable(folder: Path) -> bool:
    probe = folder / f".write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _detached() -> int:
    return getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _start(exe: Path, *args: str) -> None:
    subprocess.Popen([str(exe), *args], cwd=str(exe.parent), creationflags=_detached(), close_fds=True)


def launch(staged: Path, install: Path) -> None:
    """Starts the new version's copy, which replaces this app once it has closed."""
    _start(staged / "DusterScan.exe", "--apply-update", str(install), "--pid", str(os.getpid()),
           "--from", __version__)


def _log(text: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def _retry(fn) -> None:
    for attempt in range(RETRIES):
        try:
            return fn()
        except OSError:
            if attempt == RETRIES - 1:
                raise
            time.sleep(RETRY_S)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        _retry(lambda: shutil.rmtree(path))
    elif path.exists() or path.is_symlink():
        _retry(path.unlink)


def _wait_exit(pid: int, timeout: float) -> bool:
    """True once process `pid` has exited (or did not exist)."""
    if not pid:
        return True
    import ctypes
    synchronize, wait_object_0 = 0x00100000, 0
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == wait_object_0
    finally:
        kernel32.CloseHandle(handle)


def swap(staged: Path, install: Path) -> None:
    """Replaces the app files in `install` with the staged ones; on failure restores the old ones."""
    previous = install / PREVIOUS
    _remove(previous)   # left over from an earlier update
    previous.mkdir()
    original = [n for n in APP_FILES if (install / n).exists()]
    moved = []
    try:
        for name in original:
            _retry(lambda name=name: (install / name).rename(previous / name))
            moved.append(name)
        for name in APP_FILES:
            src = staged / name
            if src.is_dir():
                shutil.copytree(src, install / name)
            elif src.exists():
                shutil.copy2(src, install / name)
    except Exception as failure:
        problems = []
        for name in APP_FILES:
            if name in moved or name not in original:   # what is there now came from the new version
                try:
                    _remove(install / name)
                except OSError as e:
                    problems.append(e)
        for name in moved:
            try:
                _retry(lambda name=name: (previous / name).rename(install / name))
            except OSError as e:
                problems.append(e)
        if problems:
            raise OSError(f"{failure!r}; restoring the old version also failed: {problems!r}") from failure
        raise


def _options(args: list[str]) -> dict[str, str]:
    return {a: args[i + 1] for i, a in enumerate(args[:-1]) if a.startswith("--")}


def apply_main(args: list[str]) -> int:
    """Entry point of `DusterScan.exe --apply-update ...`, running from the staged new version."""
    opts = _options(args)
    install = Path(opts.get("--apply-update", ""))
    staged = Path(sys.executable).resolve().parent
    old = opts.get("--from", "?")
    _log(f"update {old} -> {__version__}: {staged} -> {install}")
    if not (install / "DusterScan.exe").is_file() or install.resolve() == staged:
        _log("not a DusterScan folder; nothing changed")
        return 2
    if not _wait_exit(int(opts.get("--pid") or 0), WAIT_EXIT_S):
        _log("the old version did not close; nothing changed")
        return 3
    try:
        swap(staged, install)
    except Exception as e:
        _log(f"failed, old version restored: {e!r}")
        _start(install / "DusterScan.exe", "--update-failed")
        return 1
    _log("installed")
    _start(install / "DusterScan.exe", "--updated", old)
    return 0


def cleanup() -> None:
    """Deletes what the last update left behind (the old app files, the download). Best effort."""
    install = install_dir()
    targets = ([install / PREVIOUS] if install else []) + [UPDATE_DIR]
    for path in targets:
        try:
            _remove(path)   # retries while the finished updater process is still closing
        except OSError:
            pass
