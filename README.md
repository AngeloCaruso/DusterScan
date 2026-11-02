# DusterScan

A Windows app that connects to the car through an ELM327-compatible adapter
(built and tested against the Vgate vLinker FS USB), scans every computer
module it can reach, writes a structured health report, and then asks Claude
for a deep analysis: what to fix, what to test, and what to keep an eye on.

Connect → Scan → Report. No digging through raw numbers.

## Read-only, by design

DusterScan never writes, clears, codes or resets anything in the car.

Every command passes through one allowlist in
[dusterscan/safety.py](dusterscan/safety.py) before it reaches the serial port.
Only explicitly listed read operations get through:

- adapter setup commands (`AT ...`) that configure the vLinker itself, not the car
- OBD-II read modes `01 02 03 06 07 09 0A`
- UDS / KWP2000 read services: read fault codes (`17 18 19`), read identification
  and data (`1A 21 22`), tester present (`3E`), and default/extended session (`10`)

Everything else is refused before a byte is sent, including clearing codes
(OBD `04`, `14`), writing data (`2E 3B 3D`), routines (`31`), security access
(`27`), actuator tests (`2F 30`), ECU reset (`11`), programming sessions and
software download (`34`-`37`). Requests are also limited to diagnostic CAN IDs.
[tests/test_safety.py](tests/test_safety.py) checks this, and the scan tests
run against a simulated car that records any non-read request and fail if there
is one.

## Using it

1. Plug the vLinker into the OBD port (under the dashboard, driver's side) and a USB port.
   Windows installs the driver by itself (it shows up as a COM port). Close DDT4All or
   FORScan if they are open, since only one program can use the port.
2. Start the engine and let it reach normal temperature (ignition ON also works, but live
   engine values are only meaningful with the engine running).
3. Unzip `DusterScan-<version>-win64.zip` anywhere (keep the folder together), open
   `DusterScan\DusterScan.exe` → **Connect** → **Start scan**.
   - **Standard** checks the engine plus the usual Duster modules (ABS, airbag, body control,
     cluster, climate, power steering, automatic gearbox) in about a minute.
   - **Deep** also searches every other diagnostic address (~3 minutes).
4. Optionally describe symptoms, then press **Analyse report**.

**Live data** (the button under the connection details) shows the engine's readings updating
continuously, with a short history line for each, until you stop it: useful for watching
the warm-up, fuel trims or O2 sensors while you rev the engine or drive. It uses the same
read-only OBD-II requests as the scan; starting a scan stops it.

Every scan is saved automatically to `Documents\DusterScan` (HTML report + JSON data), so
you can compare scans over time or re-open one with **Open saved scan**.

**Demo mode** runs the whole flow against a simulated Duster with a few faults, without an adapter.

### Languages

The whole app is available in English and Spanish: interface, scan progress, errors, the report,
fault-code and failure-type descriptions, and the AI analysis. It starts in the Windows display
language, and the selector in the top bar switches instantly, including a report that is already
open. Scans store message keys rather than finished sentences, so a saved scan opens in whichever
language is selected. The AI analysis is written in the language that was selected when it ran;
run it again after switching to get it in the other language.

To add a language, copy `dusterscan/locales/en.json` to `<code>.json`, translate the values
(keep the `{placeholders}`), and add the code to `LANGUAGES` in `dusterscan/i18n.py`.
`tests/test_i18n.py` checks that every language has every key with the same placeholders.

### AI analysis

The analysis uses Claude Opus 5.5 through the Anthropic API. Create an API key at
[console.anthropic.com](https://console.anthropic.com) and paste it in **Settings**
(it is stored in Windows Credential Manager under "DusterScan", readable only by your
Windows user; it is never written to a file).
The `ANTHROPIC_API_KEY` environment variable also works. One analysis typically costs
well under US$0.50. The analysis is written in the app's current language.

Only the scan report is sent (no scan log, no personal data beyond the VIN).
If the model declines a request, the API falls back to another Claude model automatically.

## What the report covers

- Check-engine light status, stored / pending / permanent engine codes, freeze frame
- Fault codes from every module that answers, with failure type and active/stored state
- Engine data sampled several times during the scan (fuel trims, coolant, MAF/MAP, O2
  sensors, charging voltage, ...) compared against typical warm-idle ranges
- Emission self-test (readiness) status and on-board test results (mode 06)
- Module inventory with part/software identification
- Automatic checks in plain language (lean/rich mixture, charging, thermostat, lazy O2
  sensor, codes recently cleared, ...), which work even without the AI step

## Limits worth knowing

- Fault codes from body/airbag/ABS modules are decoded with the generic SAE/ISO rules. Their
  exact Renault meaning comes from Renault's own database, which DusterScan doesn't include.
  The AI analysis interprets them and says how confident it is.
- Module live data (for example wheel speeds from the ABS, or airbag squib resistances) also
  needs that database. Engine live data uses standard OBD-II and is complete.
- Only modules on the CAN bus are scanned. If an older module on the K-line exists on this
  car, it won't be seen. DDT4All's module list for your car would show whether any exist.

## Development

Requires Python 3.11+ on Windows. There are two separate Python environments:

| Environment | Where | Used for |
|---|---|---|
| `.venv` | inside the project | running the Python code: tests, scripts, the browser preview |
| build environment | `%LOCALAPPDATA%\DusterScan-build\venv` | building the `.exe`, and opening the app window from source |

### Project environment (`.venv`)

Create or update it (installs `requirements-dev.txt`: the app's packages plus pytest and PyInstaller):

```bash
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Run the tests:

```bash
test.bat
```

Run any Python in the project with `.venv\Scripts\python.exe`, for example the browser preview,
which serves the UI with the real backend at http://127.0.0.1:8765 (no native file dialogs),
handy for UI work:

```bash
.venv\Scripts\python.exe tools\browser_preview.py
```

### Opening the app window from source

While the project lives on the WSL path (`\\wsl.localhost\...`), `.venv` cannot open the app
window: pywebview loads a .NET component from the environment, and Windows' .NET Framework
refuses to load components from network-style paths (the error is
`Failed to resolve Python.Runtime.Loader.Initialize`). Use the build environment's Python, which
lives on the local disk, instead (run `build.ps1` once first to create it):

```bash
%LOCALAPPDATA%\DusterScan-build\venv\Scripts\pythonw.exe run.py
```

If the project is moved to a normal Windows folder, `.venv\Scripts\pythonw.exe run.py` works too.

### Running from a WSL terminal

DusterScan is a Windows app (WebView2 window, Windows COM port for the adapter), and both
environments above are Windows environments. In WSL's Linux shell, activating `.venv`
(`source .venv/Scripts/activate`) changes the prompt but `python` still means WSL's Linux Python,
which fails with `ModuleNotFoundError: No module named 'webview'`. Call the Windows interpreters
by name instead; no activation needed (in the second command, `angel` is the Windows user name):

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

```bash
/mnt/c/Users/angel/AppData/Local/DusterScan-build/venv/Scripts/pythonw.exe run.py
```

A Linux virtual environment is not a substitute: without extra setup WSL can't see the USB
adapter, and the window would need a Linux GUI toolkit.

### Building the executable

Output: `dist\DusterScan\` (the app folder: `DusterScan.exe` plus its `_internal` files) and
`dist\DusterScan-<version>-win64.zip` (the same folder, zipped, for copying to other PCs).
Close the app first, because Windows locks a running `.exe`.

```bash
powershell -ExecutionPolicy Bypass -File build.ps1
```

### "Failed to resolve Python.Runtime.Loader.Initialize" on another PC

Windows marks files extracted from a downloaded or copied zip as "from the internet", and
.NET Framework then refuses to load pywebview's `Python.Runtime.dll`. The build ships
`DusterScan.exe.config` next to the `.exe` (source: `assets/DusterScan.exe.config`), which
sets `loadFromRemoteSources` so .NET trusts the app's own files. Keep that file with the
`.exe`. With an older build, right-click the zip → Properties → **Unblock** before extracting.

### Antivirus false positives

Windows Defender flagged an earlier single-file build as `Trojan:Win32/Bearfoos.A!ml`. The
`!ml` suffix means a machine-learning guess about the file's shape, not a match with known
malware; it is a common false positive for PyInstaller apps. The build avoids the traits these
models key on:

- **Folder build (`--onedir`), not a single `.exe`.** A single-file build unpacks an embedded
  archive into `%TEMP%` and runs it, the same shape as malware droppers. The folder build is a
  plain launcher next to ordinary Python files, and starts faster.
- **No UPX compression** (`--noupx`); packed executables are a strong trigger.
- **Version information** in the `.exe` (Properties > Details), so it is not anonymous.
- **No direct calls to Windows' password-decryption API.** The API key lives in Credential
  Manager through the standard `keyring` library rather than custom `CryptUnprotectData` code,
  which is the hallmark of password stealers.

If a scanner still flags a build:

1. Report it as a false positive at https://www.microsoft.com/wdsi/filesubmission (choose
   "Software developer"); Microsoft usually clears it within a few days.
2. The definitive fix is signing the `.exe` with a code-signing certificate (paid, from a
   certificate authority), which gives the file a verified publisher.
3. Rebuilding PyInstaller's bootloader from source also helps, because the stock bootloader is
   shared with malware that uses PyInstaller; it needs a C compiler.

### Layout

| File | Purpose |
|---|---|
| `dusterscan/safety.py` | Read-only allowlist. Everything sent to the adapter passes here. |
| `dusterscan/elm327.py` | Adapter driver: port auto-detection, protocol setup, ISO-TP frame reassembly |
| `dusterscan/obd2.py` | Standard OBD-II: PIDs, DTCs, readiness, VIN, mode 06 |
| `dusterscan/renault.py` | Per-module scan (UDS/KWP2000 identification and fault codes) |
| `dusterscan/dtc.py` | Code descriptions, failure types, status bits |
| `dusterscan/scanner.py` | Runs the full scan, builds the report |
| `dusterscan/rules.py` | Automatic plain-language checks |
| `dusterscan/ai.py` | Claude analysis with a strict JSON schema |
| `dusterscan/report_html.py` | Report rendering |
| `dusterscan/i18n.py`, `dusterscan/locales/` | Translations (one JSON file per language) |
| `dusterscan/app.py`, `dusterscan/ui/` | Desktop window (Edge WebView2 via pywebview) |
| `dusterscan/simulator.py` | Simulated adapter + car for demo mode and tests |
| `assets/icon.ico`, `tools/make_assets.py` | The `.exe` icon, and the script that redraws it from the app logo (needs Pillow) |
