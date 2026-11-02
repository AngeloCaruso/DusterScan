@echo off
rem Run the test suite using the project's .venv.
pushd "%~dp0"
".venv\Scripts\python.exe" -m pytest -q -p no:cacheprovider %*
set RESULT=%ERRORLEVEL%
popd
exit /b %RESULT%
