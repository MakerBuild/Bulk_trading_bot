@echo off
rem Build a zip to hand out, containing exactly what is committed.
rem
rem Built from git, not from the folder, so nothing local can leak into it: not
rem app\.venv (which carries your Windows username in pyvenv.cfg), not
rem private_key.local, not app\state, not the build caches. There is nothing to
rem remember to delete, because none of it is ever reachable.
rem
rem Lives in app\dev because the operator never runs it -- it is for whoever
rem hands the bot out. Two levels up is the project root, which is where git
rem and the archive have to be run from.

setlocal
cd /d "%~dp0..\.."

where git >nul 2>&1
if errorlevel 1 (
    echo   git is not installed -- needed to build a release.
    pause
    exit /b 1
)

rem A dirty tree would ship the last commit while you believe you shipped your
rem edits. Better to refuse than to hand out the wrong thing.
git diff --quiet && git diff --cached --quiet
if errorlevel 1 (
    echo.
    echo   There are uncommitted changes. A release is built from the last
    echo   commit, so those would NOT be in it. Commit them first:
    echo.
    git status --short
    echo.
    pause
    exit /b 1
)

for /f %%i in ('git rev-parse --short HEAD') do set REV=%%i
set OUT=..\bulkdn-%REV%.zip

if exist "%OUT%" del "%OUT%"
git archive --format=zip --output="%OUT%" HEAD
if errorlevel 1 (
    echo   Could not build the archive.
    pause
    exit /b 1
)

echo.
echo   Built %OUT%
echo.
echo   Contains only committed files -- no app\.venv, no private_key.local,
echo   no app\state, no caches. To look inside:
echo     powershell -c "Expand-Archive -Force '%OUT%' tmp; dir tmp"
echo.
echo   Whoever unzips this can run update.bat: with no .git beside it, it
echo   clones a fresh copy and lays it over the folder. What it cannot do
echo   is overwrite itself -- cmd reads a batch file as it runs it -- so
echo   they keep THIS build's update.bat for as long as they use the zip,
echo   and a later fix to the updater never reaches them.
echo.
echo   Sending the repository link instead avoids that: in a clone the
echo   merge updates update.bat along with everything else.
echo.
pause
