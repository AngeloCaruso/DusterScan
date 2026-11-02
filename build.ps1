# Builds the Windows app into dist\DusterScan\ (DusterScan.exe + its files) and
# zips it as dist\DusterScan-<version>-win64.zip for copying to other PCs.
# Nothing needs to be installed on those PCs; unzip and run DusterScan.exe.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# Why a folder and not a single .exe: a single-file PyInstaller .exe unpacks an
# embedded archive into %TEMP% and runs it, the same shape as malware droppers,
# so antivirus machine-learning models often flag it (e.g. Trojan:Win32/...!ml).
# The folder build is a plain launcher next to normal Python files: no unpacking,
# faster start, far fewer false positives. UPX compression is disabled for the
# same reason, and the .exe carries proper version information.
#
# The build virtual environment lives in %LOCALAPPDATA%\DusterScan-build so it
# works even when this folder is on a network or WSL path.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $env:LOCALAPPDATA "DusterScan-build\venv"
$work = Join-Path $env:TEMP "dusterscan-build"
$py = Join-Path $venv "Scripts\python.exe"
$dist = Join-Path $root "dist"

if (-not (Test-Path $py)) {
    Write-Host "Creating build environment..."
    py -3 -m venv $venv
}
& $py -m pip install --disable-pip-version-check -q -r "$root\requirements.txt" pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# Version resource (shown in the .exe's Properties > Details).
$version = (Select-String -Path "$root\dusterscan\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$parts = ($version.Split(".") + @("0", "0", "0", "0"))[0..3] -join ", "
New-Item -ItemType Directory -Force $work | Out-Null
$versionFile = Join-Path $work "version_info.txt"
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($parts), prodvers=($parts), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'DusterScan'),
      StringStruct('FileDescription', 'DusterScan - read-only vehicle diagnostics'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'DusterScan'),
      StringStruct('OriginalFilename', 'DusterScan.exe'),
      StringStruct('ProductName', 'DusterScan'),
      StringStruct('ProductVersion', '$version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
[IO.File]::WriteAllText($versionFile, $versionInfo, (New-Object Text.UTF8Encoding $false))  # no BOM

# Old single-file build, if any: remove it so it is not copied around by mistake.
Remove-Item (Join-Path $dist "DusterScan.exe") -ErrorAction SilentlyContinue

& $py -m PyInstaller --noconfirm --clean --onedir --windowed --noupx `
    --name DusterScan `
    --icon "$root\assets\icon.ico" `
    --version-file $versionFile `
    --add-data "$root\dusterscan\ui;dusterscan\ui" `
    --add-data "$root\dusterscan\locales;dusterscan\locales" `
    --distpath $dist --workpath $work --specpath $work `
    "$root\run.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# .NET config next to the .exe: lets the app load its own DLLs when Windows has marked
# them as downloaded (see the comment inside the file).
Copy-Item "$root\assets\DusterScan.exe.config" (Join-Path $dist "DusterScan\DusterScan.exe.config")

$zip = Join-Path $dist "DusterScan-$version-win64.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $dist "DusterScan") -DestinationPath $zip

Write-Host ""
Write-Host "Built $dist\DusterScan\DusterScan.exe"
Write-Host "Zip:  $zip"
