@echo off
rem Update to the latest version, keeping your settings and your key.
rem
rem Both are untracked, so `git pull` cannot touch them: settings.yaml is your
rem copy of settings.default.yaml, and private_key.local was never in git.

setlocal
cd /d "%~dp0"

where git >nul 2>&1
if errorlevel 1 (
    echo.
    echo   git is not installed, so this folder cannot update itself.
    echo   Get it from https://git-scm.com/downloads and run this again.
    echo.
    pause
    exit /b 1
)

if not exist ".git" (
    echo.
    echo   This copy was unzipped, not cloned, so there is nothing to update
    echo   from. To switch: clone the repository fresh, then copy your
    echo   settings.yaml and private_key.local across.
    echo.
    pause
    exit /b 1
)

echo.
echo   Fetching...
git pull --ff-only
if errorlevel 1 (
    echo.
    echo   Update failed. Usually this means a file here was edited by hand.
    echo   `git status` says which; settings.yaml and private_key.local are
    echo   never the cause -- neither is tracked.
    echo.
    pause
    exit /b 1
)

rem Dependencies can move with a release, and re-running this is cheap when
rem they have not. install.bat is safe to repeat by design.
echo.
echo   Updating dependencies...
call "%~dp0install.bat"

echo.
echo   Up to date. If settings.default.yaml gained options you want, copy
echo   them into your settings.yaml -- yours is never overwritten.
echo.
pause
