@echo off
rem Update to the latest version, keeping your settings and your key.
rem
rem Works whether this folder was cloned or unzipped. A clone pulls; an
rem unzipped copy fetches a fresh one into a temp folder and copies it over.
rem
rem Your files are never touched, and not because they are listed as exceptions
rem -- none of them exists in the repository to copy over, so there is nothing
rem to overwrite them with:
rem     settings.yaml        yours, made from app\settings.default.yaml
rem     private_key.local    yours, never in git
rem     app\state            what the bot has open
rem     app\.venv            the local Python environment
rem     logs.txt             what the bot has done

setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem The repository this checks for updates. Public, so no login is needed.
set "REPO=https://github.com/MakerBuild/Bulk_trading_bot.git"
rem ---------------------------------------------------------------------------

rem EVERYTHING BELOW IS ONE PARENTHESISED BLOCK, AND THAT IS LOAD-BEARING.
rem
rem cmd.exe reads a batch file from disk as it runs, keeping a byte offset into
rem it. `git pull` replaces this very file whenever the update changes it, and
rem cmd then carries on at that offset inside different bytes -- executing
rem whatever fragment of a line it lands in. Measured: the pull succeeded, then
rem the script printed "git is not installed" and never ran install.bat, so the
rem code updated and the dependencies did not.
rem
rem A parenthesised block is parsed in full before any of it executes, so the
rem rest of the run comes from memory and the file can change underneath it.
rem The `exit /b` at the end matters just as much: without it cmd returns to
rem reading the file after the block and lands in the shifted bytes anyway.
rem
rem Two consequences for anyone editing this:
rem   * no `goto` and no labels -- a goto abandons the block, which puts cmd
rem     back to reading the file by offset and undoes all of the above;
rem   * `%VAR%` set inside the block expands at PARSE time, before it has been
rem     set. Use !VAR!, which is why delayed expansion is on above.
rem
rem The robocopy branch also excludes this file with /XF, which protects the
rem unzipped path the same way. The usual workaround -- re-running from a copy
rem in TEMP -- is deliberately not used: a batch file that copies itself to
rem TEMP and then fetches from the network is what a self-replicating script
rem looks like, and Windows Defender deleted this file on write, twice, while
rem it did that.
(
    where git >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   git is not installed. Get it from https://git-scm.com/downloads,
        echo   keep every default, then run this again.
        echo.
        pause
        exit /b 1
    )

    if exist ".git" (
        rem -- cloned copy: a pull is enough ------------------------------
        echo.
        echo   Fetching...
        git pull --ff-only
        if errorlevel 1 (
            echo.
            echo   Update failed. Almost always this means a file that ships
            echo   with the bot was edited by hand, and git will not overwrite
            echo   your edit without being told to. The line above names it.
            echo.
            echo   Your own files are never the cause: settings.yaml, your key,
            echo   app\state and logs.txt are not tracked, so an update has
            echo   nothing to overwrite them with.
            echo.
            echo   To throw away edits to the shipped files and update anyway:
            echo.
            echo       git checkout -- .
            echo       update.bat
            echo.
            pause
            exit /b 1
        )
    ) else (
        rem Pure batch, deliberately: `find` here would be whichever find is
        rem first on PATH, and on a machine with Git Bash installed that is
        rem the Unix one.
        if not "!REPO!"=="!REPO:CHANGE-ME=!" (
            echo.
            echo   This build has no repository set, so it cannot fetch updates.
            echo   Ask whoever sent it to you for a build that does.
            echo.
            pause
            exit /b 1
        )

        rem -- unzipped copy: fetch a fresh one and copy it over -----------
        set "TMPDIR=%TEMP%\bulkdn-update-!RANDOM!"
        echo.
        echo   Downloading the latest version...
        git clone --depth 1 --quiet "!REPO!" "!TMPDIR!"
        if errorlevel 1 (
            echo.
            echo   Could not reach the repository. Check your internet connection.
            echo.
            if exist "!TMPDIR!" rd /s /q "!TMPDIR!"
            pause
            exit /b 1
        )

        echo   Installing it...
        rem /E copies everything; /XD .git leaves the clone's own history
        rem behind. No /PURGE, so anything here that is not in the repository
        rem simply stays. /XF update.bat: see the note above the block.
        robocopy "!TMPDIR!" "." /E /XD ".git" /XF "update.bat" /NFL /NDL /NJH /NJS /NP >nul
        rem robocopy returns 0-7 for success of various kinds; 8 and up is a
        rem failure.
        if errorlevel 8 (
            echo   Copy failed.
            rd /s /q "!TMPDIR!"
            pause
            exit /b 1
        )
        rd /s /q "!TMPDIR!"
    )

    rem Files that moved in a later version. Neither a pull nor a robocopy
    rem deletes anything, so without this the old copy sits in the root looking
    rem current. Only ever list files that shipped with the bot -- never
    rem anything of the operator's.
    if exist "release.bat" if exist "app\dev\release.bat" del "release.bat"
    if exist "settings.default.yaml" if exist "app\settings.default.yaml" del "settings.default.yaml"

    rem Dependencies can move with a release, and re-running this is cheap when
    rem they have not. install.bat is safe to repeat by design.
    echo.
    echo   Updating dependencies...
    call "%~dp0install.bat"

    echo.
    echo   Up to date. Your settings and key were left alone.
    echo   If app\settings.default.yaml gained options you want, copy them across.
    echo.
    pause
    exit /b 0
)
