@echo off
rem Build a zip to hand out.
rem
rem Built from a fresh clone of the published repository, never from this
rem folder. Two reasons, and the first is the one that matters:
rem
rem   * Nothing local can reach the zip. Not app\.venv, which carries your
rem     Windows username inside pyvenv.cfg, not private_key.local, not
rem     app\state, not logs.txt, not the caches. There is nothing to remember
rem     to delete, because none of it is anywhere near what gets packed. Local
rem     branches included: the checkout this was written on carried two backup
rem     branches from a history rewrite, seventeen and seventy-four commits,
rem     every one of them still signed with the author's real name and address.
rem     Zipping the folder by hand would have handed that to everybody.
rem
rem   * The zip contains a .git, so what the operator unpacks is a clone, and
rem     update.bat there takes the clone path -- where the merge replaces
rem     update.bat along with everything else. A copy without .git can never
rem     repair its own updater: robocopy has to exclude the file it is running
rem     from, because cmd reads a batch file as it executes it.
rem
rem It also means the zip is exactly what the repository serves. If you have
rem not pushed, this refuses rather than shipping a version nobody can pull.
rem
rem Lives in app\dev because the operator never runs it -- it is for whoever
rem hands the bot out. Two levels up is the project root, which is where git
rem has to run from.

setlocal enabledelayedexpansion
cd /d "%~dp0..\.."

where git >nul 2>&1
if errorlevel 1 (
    echo   git is not installed -- needed to build a release.
    pause
    exit /b 1
)

rem Uncommitted work is not in any commit, and therefore not in the clone this
rem builds from. Better to refuse than to hand out a zip that silently lacks
rem the very change you meant to ship.
git diff --quiet && git diff --cached --quiet
if errorlevel 1 (
    echo.
    echo   There are uncommitted changes. This builds from what GitHub serves,
    echo   so they would NOT be in it. Commit and push them first:
    echo.
    git status --short
    echo.
    pause
    exit /b 1
)

for /f "usebackq tokens=*" %%i in (`git config --get remote.origin.url`) do set "REPO=%%i"
if not defined REPO (
    echo   This checkout has no origin, so there is nothing to clone from.
    pause
    exit /b 1
)

echo.
echo   Checking what GitHub has...
set "FETCHED="
for /L %%i in (1,1,3) do (
    if not defined FETCHED (
        if %%i GTR 1 (
            echo   No answer. Trying again ^(%%i of 3^)...
            ping -n 3 127.0.0.1 >nul
        )
        git fetch --quiet origin
        if not errorlevel 1 set "FETCHED=1"
    )
)
if not defined FETCHED (
    echo.
    echo   Could not reach GitHub after three tries. Nothing was built.
    echo.
    pause
    exit /b 1
)

for /f "usebackq tokens=*" %%i in (`git rev-parse HEAD`) do set "LOCAL=%%i"
for /f "usebackq tokens=*" %%i in (`git rev-parse origin/main`) do set "PUBLISHED=%%i"
if not "!LOCAL!"=="!PUBLISHED!" (
    echo.
    echo   This checkout and origin/main are not on the same commit, so the
    echo   zip would not be the version you are looking at. Push first, then
    echo   run this again.
    echo.
    echo   here:
    git log --oneline -1 HEAD
    echo   GitHub:
    git log --oneline -1 origin/main
    echo.
    pause
    exit /b 1
)

for /f "usebackq tokens=*" %%i in (`git rev-parse --short HEAD`) do set "REV=%%i"
set "TMPDIR=%TEMP%\bulkdn-release-!RANDOM!"
rem An absolute path, because the zip is written by a .NET call that resolves
rem relative paths against its own working directory, not this one.
for %%A in ("..\bulkdn-!REV!.zip") do set "OUT=%%~fA"

echo   Cloning !REV! ...
set "CLONED="
for /L %%i in (1,1,3) do (
    if not defined CLONED (
        if %%i GTR 1 (
            echo   No answer. Trying again ^(%%i of 3^)...
            ping -n 3 127.0.0.1 >nul
            if exist "!TMPDIR!" rd /s /q "!TMPDIR!"
        )
        git clone --quiet "!REPO!" "!TMPDIR!"
        if not errorlevel 1 set "CLONED=1"
    )
)
if not defined CLONED (
    echo.
    echo   Could not clone the repository. Nothing was built.
    if exist "!TMPDIR!" rd /s /q "!TMPDIR!"
    echo.
    pause
    exit /b 1
)

rem What the clone checked out is what ships, so it is what gets verified --
rem not what this folder happens to be sitting on.
for /f "usebackq tokens=*" %%i in (`git -C "!TMPDIR!" rev-parse HEAD`) do set "CLONEHEAD=%%i"
if not "!CLONEHEAD!"=="!LOCAL!" (
    echo   The clone came back on a different commit. Nothing was built.
    rd /s /q "!TMPDIR!"
    pause
    exit /b 1
)

if exist "!OUT!" del "!OUT!"
rem Compress-Archive is deliberately not used: it has a long history of
rem skipping hidden entries, and git marks .git hidden on Windows -- which
rem would quietly produce exactly the zip this script exists to avoid. The
rem .NET call takes the directory whole.
powershell -NoProfile -Command "Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::CreateFromDirectory('!TMPDIR!', '!OUT!')"
if errorlevel 1 (
    echo   Could not build the archive.
    rd /s /q "!TMPDIR!"
    pause
    exit /b 1
)
rd /s /q "!TMPDIR!"

if not exist "!OUT!" (
    echo   The archive is not where it should be. Nothing was built.
    pause
    exit /b 1
)

echo.
echo   Built !OUT!
echo.
echo   A clone of !REV!, straight from GitHub -- so nothing of yours is in it,
echo   and the operator who unpacks it can update in place: update.bat there
echo   fetches and merges, which replaces update.bat too.
echo.
echo   To look inside:
echo     powershell -c "Expand-Archive -Force '!OUT!' tmp; dir tmp -Force"
echo.
pause
