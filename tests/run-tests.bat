@echo off
rem Run the test suite. Lives in tests/ rather than the project root because
rem it is for whoever works on the bot, not for whoever runs it.
rem
rem The pytest and ruff settings live in dev/, so the root holds only the
rem files an operator actually touches.

setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo   Not installed yet. Run install.bat first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m pytest -c dev\pytest.ini %*
set RESULT=%ERRORLEVEL%

.venv\Scripts\python.exe -m ruff check --config dev\ruff.toml bulkdn tests
exit /b %RESULT%
