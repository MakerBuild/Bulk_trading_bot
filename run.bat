@echo off
rem Launch the bot with the project's own interpreter.
rem
rem Typing `python -m bulkdn` picks up whatever Python is first on PATH, which
rem is usually a global install without the SDK -- so the bot refuses to start.
rem This removes the choice.
rem
rem Works from any directory and passes arguments through, so `run.bat status`
rem and `run.bat run --live` behave like the CLI. With no arguments the menu
rem opens.

setlocal
cd /d "%~dp0"

set "VENV_PY=%~dp0app\.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo.
    echo   Not installed yet. Run install.bat first.
    echo.
    pause
    exit /b 1
)

rem The package lives in app\, which is not where Python looks by default.
set "PYTHONPATH=%~dp0app"

"%VENV_PY%" -m bulkdn %*
exit /b %ERRORLEVEL%
