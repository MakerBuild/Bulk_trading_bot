@echo off
rem Update to the latest version, keeping your settings and your key.
rem
rem Works whether this folder was cloned or unzipped. A clone pulls; an
rem unzipped copy fetches a fresh one into a temp folder and copies it over.
rem
rem Your files are never touched, and not because they are listed as exceptions
rem -- none of them exists in the repository to copy over, so there is nothing
rem to overwrite them with:
rem     settings.yaml        yours, made from settings.default.yaml
rem     private_key.local    yours, never in git
rem     app\state            what the bot has open
rem     app\.venv            the local Python environment
rem     logs.txt             what the bot has done

setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem The repository this checks for updates. Public, so no login is needed.
set "REPO=https://github.com/MakerBuild/Bulk_trading_bot.git"
rem ---------------------------------------------------------------------------

where git >nul 2>&1
if errorlevel 1 (
    echo.
    echo   git is not installed. Get it from https://git-scm.com/downloads,
    echo   keep every default, then run this again.
    echo.
    pause
    exit /b 1
)

if exist ".git" goto :pull

rem Pure batch, deliberately: `find` here would be whichever find is first on
rem PATH, and on a machine with Git Bash installed that is the Unix one.
if not "%REPO%"=="%REPO:CHANGE-ME=%" (
    echo.
    echo   This build has no repository set, so it cannot fetch updates.
    echo   Ask whoever sent it to you for a build that does.
    echo.
    pause
    exit /b 1
)

rem -- unzipped copy: fetch a fresh one and copy it over ----------------------
set "TMPDIR=%TEMP%\bulkdn-update-%RANDOM%"
echo.
echo   Downloading the latest version...
git clone --depth 1 --quiet "%REPO%" "%TMPDIR%"
if errorlevel 1 (
    echo.
    echo   Could not reach the repository. Check your internet connection.
    echo.
    if exist "%TMPDIR%" rd /s /q "%TMPDIR%"
    pause
    exit /b 1
)

echo   Installing it...
rem /E copies everything; /XD .git leaves the clone's own history behind. No
rem /PURGE, so anything here that is not in the repository simply stays.
rem
rem /XF update.bat matters: cmd reads a batch file from disk as it runs,
rem keeping a byte offset into it. Replacing this file mid-run leaves cmd
rem carrying on at that offset inside different bytes, executing whatever
rem fragment of a line it lands in -- seen as "'EL' is not recognized" from
rem a line that is a plain comment.
rem
rem The usual workaround is to re-run from a copy in TEMP. Do not: a batch
rem file that copies itself to TEMP and then fetches from the network is
rem what a self-replicating script looks like, and Windows Defender deleted
rem this file on write, twice, while it did that. So this script never
rem updates itself; if it ever must change, that needs a fresh zip.
robocopy "%TMPDIR%" "." /E /XD ".git" /XF "update.bat" /NFL /NDL /NJH /NJS /NP >nul
rem robocopy returns 0-7 for success of various kinds; 8 and up is failure.
if errorlevel 8 (
    echo   Copy failed.
    rd /s /q "%TMPDIR%"
    pause
    exit /b 1
)
rd /s /q "%TMPDIR%"
goto :deps

rem -- cloned copy: a pull is enough -----------------------------------------
:pull
echo.
echo   Fetching...
git pull --ff-only
if errorlevel 1 (
    echo.
    echo   Update failed. Usually a tracked file was edited by hand; `git status`
    echo   says which. Your settings and key are never the cause -- neither is
    echo   tracked.
    echo.
    pause
    exit /b 1
)

:deps
rem Files that moved in a later version. Neither a pull nor a robocopy deletes
rem anything, so without this the old copy sits in the root looking current.
rem Only ever list files that shipped with the bot -- never anything of the
rem operator's.
if exist "release.bat" if exist "app\dev\release.bat" del "release.bat"

rem Dependencies can move with a release, and re-running this is cheap when they
rem have not. install.bat is safe to repeat by design.
echo.
echo   Updating dependencies...
call "%~dp0install.bat"

echo.
echo   Up to date. Your settings and key were left alone.
echo   If settings.default.yaml gained options you want, copy them across.
echo.
pause
