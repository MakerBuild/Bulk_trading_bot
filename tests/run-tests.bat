@echo off
rem Run the test suite. Lives in tests/ rather than the project root because
rem it is for whoever works on the bot, not for whoever runs it.

setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo   Not installed yet. Run install.bat first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m pytest %*
exit /b %ERRORLEVEL%
