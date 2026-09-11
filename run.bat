@echo off
rem Launch the bot with the project's own interpreter.
rem
rem Typing `python main.py` picks up whatever Python is first on PATH, which on
rem this machine is a global install carrying an older bulk_api without
rem SignatureDomain -- so the bot refuses to start. This removes the choice.
rem
rem Works from any directory and passes arguments through, so `run.bat status`
rem and `run.bat run --live` behave like the CLI.

setlocal
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo.
    echo   The project virtualenv is missing: %VENV_PY%
    echo.
    echo   Create it and install the dependencies:
    echo.
    echo     python -m venv --system-site-packages .venv
    echo     .venv\Scripts\python -m pip install --no-deps -e .
    echo     .venv\Scripts\python -m pip install pyyaml requests aiohttp
    echo.
    exit /b 1
)

"%VENV_PY%" main.py %*
exit /b %ERRORLEVEL%
