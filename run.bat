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
set "CODE=%ERRORLEVEL%"
rem Kept open on a failure. A window opened by double-clicking run.bat
rem closes the moment this script ends, and with it went the only copy
rem of whatever stopped the bot -- the guide had to tell people to open a
rem command prompt and run it from there just to read one line. The
rem reason is in logs.txt as well. Exit code 0 closes as before.
if not "%CODE%"=="0" (
    echo.
    echo   The bot stopped with an error ^(code %CODE%^). The reason is above,
    echo   and in logs.txt.
    pause
)
exit /b %CODE%
