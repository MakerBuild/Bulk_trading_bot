@echo off
rem Run the test suite. Lives in tests/ rather than the project root because
rem it is for whoever works on the bot, not for whoever runs it.
rem
rem Everything the operator does not touch lives under app/, so this walks up
rem two levels to the project root and works from there.

setlocal
cd /d "%~dp0..\.."

if not exist "app\.venv\Scripts\python.exe" (
    echo   Not installed yet. Run install.bat first.
    pause
    exit /b 1
)

rem The package is app\bulkdn, so app\ is what has to be importable.
set "PYTHONPATH=%CD%\app"

app\.venv\Scripts\python.exe -m pytest -c app\dev\pytest.ini %*
set RESULT=%ERRORLEVEL%

app\.venv\Scripts\python.exe -m ruff check --config app\dev\ruff.toml app\bulkdn app\tests
exit /b %RESULT%
