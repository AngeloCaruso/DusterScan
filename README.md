# DusterScan

[![Build](https://github.com/AngeloCaruso/DusterScan/actions/workflows/build.yml/badge.svg)](https://github.com/AngeloCaruso/DusterScan/actions/workflows/build.yml)

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
3. Download `DusterScan-<version>-win64.zip` from
   [Releases](https://github.com/AngeloCaruso/DusterScan/releases/latest) and unzip it in a folder of
   yours, such as Documents or the Desktop (keep the folder together; not Program Files, so the app
   can update itself). Open `DusterScan\DusterScan.exe` → **Connect** → **Start scan**.
   - **Standard** checks the engine plus the usual Duster modules (ABS, airbag, body control,
     cluster, climate, power steering, automatic gearbox) in about a minute.
   - **Deep** also searches every other diagnostic address (~3 minutes).
4. Optionally describe symptoms, then press **Analyse report**.

**Settings** (top right) holds the three things you set once: the Anthropic API key, the
**odometer** (optional: it lets the report say how long ago each fault code was stored; when a
module reports the mileage through the Renault database, that reading is used instead, so a
number typed weeks ago never overrides the car) and the **Renault module database**.

**Live data** (the button under the connection details) shows readings updating
continuously, with a short history line for each, until you stop it: the engine over OBD-II,
or, with the Renault database, any module that was read with a definition (pick the module
and one of its screens, for example the gearbox oil temperature). It uses the same read-only
requests as the scan; starting a scan stops it.

**Battery test** (next to Live data) is a one-minute guided test: battery at rest with the
engine off, the lowest voltage while you start the engine (the best indicator of battery
health), then charging at idle and with the lights, blower, defrost and A/C on. It only reads
voltages. The result goes into the report, and the automatic checks use it instead of the
scan's two voltage readings.

### Renault module database (optional)

Without it, modules are read with the generic standards: every fault code, but only a generic
meaning for Renault's own codes. With DDT4All's database (`ecu.zip`), DusterScan understands
each module the way Renault's tools do:

- Renault's name for each fault code, the mileage when it was stored and how many times it
  happened (so an old one-off event is told apart from a recurring fault);
- the module's own measurements, states and configuration (gearbox oil temperature, wheel
  speeds, airbag circuit resistances, module supply voltages, ...), in a "Module data" section;
- the odometer, from the module that stores it (instead of the number in Settings).

Import it once: **Settings → Renault module database → Import .zip**. DusterScan keeps its own
copy in `%LOCALAPPDATA%\DusterScan\ddt` (you can move or delete the original) and never
bundles it or sends it anywhere; only decoded values go into reports and the AI analysis.

After that, every scan matches each module to a definition by its identification (diagnostic
version, supplier, software, edition). Only an **exact** match, or one where only the edition
differs (**same software**), is used automatically; anything less certain keeps the module
**generic**, exactly as without the database. The **Module definitions** card (under Scan)
shows what was chosen and lets you pick another definition from a searchable list, which
re-reads that module; your pick is remembered for that module. Even then the answers are
checked against the definition, and a definition they don't fit is dropped for that scan.

A **standard** scan reads each matched module's most informative values within a time
budget; a **deep** scan reads every value it can. Only read requests are taken from a
definition (the files also describe writes, actuator tests and resets: those are never
sent, and `safety.py` refuses them anyway).

Every scan is saved automatically to `Documents\DusterScan` (HTML report + JSON data), so
you can compare scans over time or re-open one with **Open saved scan**.

**Demo mode** runs the whole flow against a simulated Duster with a few faults, without an adapter.

### Updates

At start, DusterScan asks GitHub once for the latest release. When there is a newer one, an
**Update** button appears next to the version in the top bar (Settings → App version shows the same
and has **Check for updates**). It opens what changed; **Update now** downloads the new version,
checks it against the release's SHA-256 checksum, closes the app, replaces the app files in its
folder and starts the new version. Nothing installs without that click.

- Only the app files change (`DusterScan.exe`, `DusterScan.exe.config`, `_internal`). Settings, the
  API key, the Renault database and saved scans live elsewhere and stay as they are.
- If anything goes wrong while replacing the files, the old version is put back and started again;
  the details are in `%LOCALAPPDATA%\DusterScan\update.log`.
- It contacts only GitHub (api.github.com and the release download), and sends nothing about the
  car or your reports. Without internet nothing is shown.
- Updates need the app folder to be writable (not under Program Files). Running from source, the
  app shows the new version but you update with `git pull`.
- Versions before 0.5.0 have no updater: download 0.5.0 from Releases once.

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
- Module inventory with part/software identification, and which Renault definition read each
- With the Renault database: Renault code names, when each code was stored and how often, and
  each module's own values
- The battery and charging test, when one was made on this connection
- Automatic checks in plain language (lean/rich mixture, charging, thermostat, lazy O2
  sensor, codes recently cleared, network/voltage events, a module with a poor supply,
  battery health, ...), which work even without the AI step

## Limits worth knowing

- Without the Renault database, fault codes from body/airbag/ABS modules are decoded with the
  generic SAE/ISO rules and module values are not read; the AI analysis interprets the codes
  and says how confident it is. With it, a module whose software is not in the database (on a
  2018 Duster, the ABS) stays generic unless you pick a definition yourself.
- Only modules on the CAN bus are scanned. If an older module on the K-line exists on this
  car, it won't be seen. DDT4All's module list for your car would show whether any exist.

## Development

Requires Python 3.11+ on Windows, and the project in a normal Windows folder (on a network or
WSL path, Windows' .NET Framework refuses to load pywebview's component and the window fails
with `Failed to resolve Python.Runtime.Loader.Initialize`). Everything runs in one virtual
environment, `.venv`, inside the project.

Create or update it (installs `requirements-dev.txt`: the app's packages plus pytest and PyInstaller):

```bash
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Open the app from source:

```bash
run.bat
```

Run the tests:

```bash
test.bat
```

The browser preview serves the UI with the real backend at http://127.0.0.1:8765 (no native
file dialogs), handy for UI work:

```bash
.venv\Scripts\python.exe tools\browser_preview.py
```

### Building the executable

Output: `dist\DusterScan\` (the app folder: `DusterScan.exe` plus its `_internal` files) and
`dist\DusterScan-<version>-win64.zip` (the same folder, zipped, for copying to other PCs).
Close the app first, because Windows locks a running `.exe`. The build uses `.venv` too.

```bash
powershell -ExecutionPolicy Bypass -File build.ps1
```

### Releases (GitHub Actions)

[.github/workflows/build.yml](.github/workflows/build.yml) runs on every push and pull request, on
a Windows runner: it creates `.venv`, runs `test.bat`, runs `build.ps1`, starts the built app with
`--self-check` (it must find its UI files, languages, credential store and .NET parts) and keeps the
zip as a downloadable artifact of the run.

To publish a version, change `__version__` in [dusterscan/__init__.py](dusterscan/__init__.py)
(three numbers, like `0.5.1`) and push to main. If there is no release `v<version>` yet, the
pipeline creates it with `DusterScan-<version>-win64.zip`, `SHA256SUMS.txt` and the commit subjects
since the previous release as notes. Pushes that don't change the version only test and build. The
installed apps then offer the update by themselves (see Updates).

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
| `dusterscan/live.py` | Live data: continuous engine or module readings until stopped |
| `dusterscan/battery.py` | Guided battery and charging test |
| `dusterscan/ddt/` | Renault database: import (`store.py`), matching (`match.py`), decoding (`decode.py`), read-only module reading (`reader.py`), demo definitions (`demo.py`) |
| `dusterscan/rules.py` | Automatic plain-language checks |
| `dusterscan/ai.py` | Claude analysis with a strict JSON schema |
| `dusterscan/report_html.py` | Report rendering |
| `dusterscan/i18n.py`, `dusterscan/locales/` | Translations (one JSON file per language) |
| `dusterscan/app.py`, `dusterscan/ui/` | Desktop window (Edge WebView2 via pywebview) |
| `dusterscan/updater.py` | Updates from GitHub releases: check, verified download, file swap with rollback |
| `.github/workflows/build.yml` | Test, build and release pipeline |
| `dusterscan/simulator.py` | Simulated adapter + car for demo mode and tests |
| `assets/icon.ico`, `tools/make_assets.py` | The `.exe` icon, and the script that redraws it from the app logo (needs Pillow) |
