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

rem install.bat deliberately does not install these: someone running the
rem bot has no use for them. Say what is missing rather than failing with
rem "No module named pytest", which reads like a broken install.
app\.venv\Scripts\python.exe -c "import pytest, ruff" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   The test tools are not installed. install.bat leaves them out
    echo   because running the bot does not need them. To add them:
    echo.
    echo     app\.venv\Scripts\python.exe -m pip install pytest pytest-asyncio ruff
    echo.
    pause
    exit /b 1
)

app\.venv\Scripts\python.exe -m pytest -c app\dev\pytest.ini %*
set RESULT=%ERRORLEVEL%

app\.venv\Scripts\python.exe -m ruff check --config app\dev\ruff.toml app\bulkdn app\tests
exit /b %RESULT%
