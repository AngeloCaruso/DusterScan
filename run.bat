@echo off
rem Open the app from source using the project's .venv (create it first with setup.ps1).
pushd "%~dp0"
start "" ".venv\Scripts\pythonw.exe" run.py
popd
