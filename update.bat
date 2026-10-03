@echo off
rem Update to the latest version, keeping your settings and your key.
rem
rem Works whether this folder was cloned or unzipped. A clone fetches and
rem fast-forwards; an unzipped copy is first turned into a clone, then updated
rem the same way -- see below.
rem
rem Your files are never touched, and not because they are listed as exceptions
rem -- none of them exists in the repository to copy over, so there is nothing
rem to overwrite them with:
rem     settings.yaml        yours, made from app\settings.default.yaml
rem     private_key.local    yours, never in git
rem     app\state            what the bot has open
rem     app\.venv            the local Python environment
rem     logs.txt             what the bot has done

rem The folder first, delayed expansion after: with it on, a `!` in the path
rem is read as a variable reference and deleted. See install.bat.
setlocal
cd /d "%~dp0"
setlocal enabledelayedexpansion

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
rem The usual workaround -- re-running from a copy in TEMP -- is deliberately
rem not used: a batch file that copies itself to TEMP and then fetches from the
rem network is what a self-replicating script looks like, and Windows Defender
rem deleted this file on write, twice, while it did that.
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

    rem Not while the bot is running from this folder. An update replaces the
    rem code under it and install.bat reinstalls the libraries it has loaded.
    rem bulkdn\lock.py answers from the standard library alone and exits 3 when
    rem the bot holds its lock; a Python that cannot run it exits 1, which is
    rem not taken as "running".
    set "PYTHONPATH=app"
    if exist "app\.venv\Scripts\python.exe" (
        app\.venv\Scripts\python.exe -m bulkdn.lock
        if errorlevel 3 (
            echo.
            echo   Stop the bot first -- close its window, or stop Telegram
            echo   control -- then run this again. Nothing was changed.
            echo.
            pause
            exit /b 1
        )
    )

    rem The bot already reads proxy.local, because BULK is unreachable from
    rem some countries. GitHub is unreachable from the same ones, and an update
    rem that cannot fetch leaves the operator as stuck as a bot that cannot
    rem trade. Measured on the network this was written for: three direct
    rem attempts to github.com failed outright, two through the proxy answered
    rem in 2.5 seconds.
    rem
    rem ALL_PROXY and nothing else. git reads it -- confirmed against a live
    rem fetch with the other two unset. The line is never echoed: it usually
    rem carries a password.
    rem
    rem The address comes from the bot's own reader, so git uses exactly what
    rem the bot does. This used to parse the file itself: the last usable
    rem line, trailing spaces kept, and an indented `  # note` taken for an
    rem address. The bot's virtualenv runs it, or the Python on PATH when there
    rem is none yet. Read with delayed expansion OFF, exactly as install.bat
    rem does and for the same reason: with it on, every ! in a proxy password
    rem is deleted. The interpreter reaches the inner loop as %%P, which needs
    rem no expansion. The scope is left open, not closed -- closing it would
    rem discard BOT_PROXY -- and delayed expansion is switched back on inside
    rem it for the rest of this block, which reads it only as !BOT_PROXY!.
    set "BOT_PROXY="
    set "TOOL_PY="
    if exist "app\.venv\Scripts\python.exe" (
        set "TOOL_PY=app\.venv\Scripts\python.exe"
    ) else (
        where python >nul 2>&1
        if not errorlevel 1 set "TOOL_PY=python"
    )
    if exist "proxy.local" if defined TOOL_PY (
        !TOOL_PY! -m bulkdn.scripts proxy >nul
        if errorlevel 1 (
            echo.
            echo   proxy.local cannot be used -- the reason is above. Fix it, or
            echo   empty it, and run this again. Nothing was changed.
            echo.
            pause
            exit /b 1
        )
        for %%P in ("!TOOL_PY!") do (
            setlocal DisableDelayedExpansion
            for /f "usebackq delims=" %%L in (`%%~P -m bulkdn.scripts proxy 2^>nul`) do set "BOT_PROXY=%%L"
        )
        setlocal EnableDelayedExpansion
    )
    if defined BOT_PROXY (
        echo   Using the proxy from proxy.local.
        set "ALL_PROXY=!BOT_PROXY!"
    )

    if exist ".git" (
        rem -- cloned copy: fetch, then merge -----------------------------
        rem
        rem Deliberately not `git pull`. A pull fails for two unrelated
        rem reasons and reports them identically, and the message this
        rem printed blamed a hand-edited file for what was a dead network:
        rem github.com resolves to several addresses and one of them was
        rem unreachable, so the update failed about every other run while
        rem telling the operator to throw away edits they had not made.
        rem Retried, because a single attempt is not evidence of anything.
        rem github.com answers on several addresses and the route to some of
        rem them drops packets on some networks: measured here, the same
        rem address timed out after 21s and then answered in 1.3s. Making the
        rem operator run this again by hand is asking them to do what the
        rem script can do itself.
        echo.
        echo   Fetching...
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
            echo   Could not reach GitHub after three tries, so there is
            echo   nothing to update from. Nothing was changed.
            echo.
            echo   Check your connection and run this again. The bot itself
            echo   is unaffected -- this only fetches new code.
            echo.
            pause
            exit /b 1
        )

        git merge --ff-only FETCH_HEAD
        if errorlevel 1 (
            rem A fast-forward fails for two different reasons, and the fix
            rem for one does nothing for the other -- the old message offered
            rem `git checkout -- .` for both, which cannot repair a diverged
            rem history and silently destroys uncommitted edits. So: work out
            rem which it is before saying anything.
            rem
            rem   diverged     this copy has commits GitHub does not, and GitHub
            rem                has commits this copy does not: nothing to
            rem                fast-forward. Counted both ways with rev-list.
            rem   uncommitted  a shipped file was edited, or a file is sitting
            rem                where the update wants to put one. Read from
            rem                status --porcelain, which leaves out ignored
            rem                files -- settings.yaml, the key, app\state.
            set "AHEAD=0"
            set "BEHIND=0"
            set "DIRTY="
            set "DIVERGED="
            for /f %%n in ('git rev-list --count FETCH_HEAD..HEAD 2^>nul') do set "AHEAD=%%n"
            for /f %%n in ('git rev-list --count HEAD..FETCH_HEAD 2^>nul') do set "BEHIND=%%n"
            if !AHEAD! GTR 0 if !BEHIND! GTR 0 set "DIVERGED=1"
            for /f "delims=" %%s in ('git status --porcelain 2^>nul') do set "DIRTY=1"
            echo.
            echo   Downloaded, but could not apply it. Nothing was changed.
            echo.
            echo   Your own files are never the cause: settings.yaml, your key,
            echo   app\state and logs.txt are not tracked, so an update has
            echo   nothing to overwrite them with.
            echo.
            if defined DIVERGED (
                echo   This copy has !AHEAD! commit^(s^) of its own that GitHub does not
                echo   have, and GitHub has !BEHIND! new one^(s^). Git will not guess how
                echo   to combine them. To keep yours on a branch of their own and
                echo   move this folder to the published version:
                echo.
                echo       git branch my-changes
                echo       git reset --keep FETCH_HEAD
                echo       update.bat
                echo.
                echo   Nothing is lost: your commits stay on my-changes, and
                echo   --keep refuses rather than overwrite a file you have
                echo   edited but not committed.
                echo.
            )
            if defined DIRTY (
                echo   These files that ship with the bot were edited by hand, or
                echo   are new and in the way of the update:
                echo.
                git status --short
                echo.
                echo   To set your edits aside, update, and then put them back:
                echo.
                echo       git stash push --include-untracked
                echo       update.bat
                echo       git stash pop
                echo.
                echo   If the pop reports a conflict, your edit and the update
                echo   changed the same lines. Git keeps the stash until you have
                echo   sorted it out, so nothing is lost.
                echo.
                echo   Or, ONLY if you do not want those edits: the command below
                echo   DELETES every uncommitted change to the shipped files, for
                echo   good. There is no undo.
                echo.
                echo       git checkout -- .
                echo.
            )
            if not defined DIRTY if not defined DIVERGED (
                echo   The message from git above says why. To see where this
                echo   copy stands:
                echo.
                echo       git status
                echo.
            )
            pause
            exit /b 1
        )
    ) else (
        rem -- unzipped copy: make it a clone, then it is one ---------------
        rem
        rem It used to download a fresh copy and robocopy it over this one.
        rem That copy skipped this file (cmd is reading it), so an unzipped
        rem copy never received a fix to its own updater; it deleted nothing,
        rem so a module removed upstream stayed behind looking current; and a
        rem hand-kept list of files to delete -- one of them a settings file,
        rem with no backup -- stood in for what git does on its own.
        rem
        rem Now the folder becomes a clone in place. `checkout -f` writes every
        rem shipped file over the unzipped one and leaves everything else
        rem alone: settings.yaml, the key, proxy.local, app\state and logs.txt
        rem are not in the repository, so there is nothing to overwrite them
        rem with. They are copied into update-backup first all the same.
        echo.
        echo   This copy was unzipped rather than cloned. Turning it into a
        echo   clone, so this and every later update works the same way...
        if not exist "update-backup" mkdir "update-backup"
        for %%F in (settings.yaml private_key.local proxy.local settings.default.yaml) do (
            if exist "%%F" copy /y "%%F" "update-backup\%%F" >nul
        )
        if exist "app\state" robocopy "app\state" "update-backup\state" /E /NFL /NDL /NJH /NJS /NP >nul
        echo   Your settings, key and state are also copied to update-backup.

        git init --quiet
        git remote remove origin >nul 2>&1
        git remote add origin "!REPO!"
        rem Ignored by this clone alone, so it never shows as a change.
        >>".git\info\exclude" echo /update-backup/
        set "FETCHED="
        for /L %%i in (1,1,3) do (
            if not defined FETCHED (
                if %%i GTR 1 (
                    echo   No answer. Trying again ^(%%i of 3^)...
                    ping -n 3 127.0.0.1 >nul
                )
                git fetch --quiet origin main
                if not errorlevel 1 set "FETCHED=1"
            )
        )
        if not defined FETCHED (
            echo.
            echo   Could not reach GitHub after three tries, so there is
            echo   nothing to update from. Your files were not changed.
            echo.
            pause
            exit /b 1
        )
        git checkout --quiet -f -B main FETCH_HEAD
        if errorlevel 1 (
            echo.
            echo   Downloaded, but could not apply it -- see the message above.
            echo   Your settings, key and state are in update-backup.
            echo.
            pause
            exit /b 1
        )
        rem So the next update's plain `git fetch origin` knows what to merge.
        git config branch.main.remote origin
        git config branch.main.merge refs/heads/main
        rem What an unzipped copy kept of modules since removed. Only the
        rem bot's own code folders -- nothing of the operator's lives there.
        git clean -fdq -- app\bulkdn app\tests app\dev
        rem Files that moved out of the root in an older version. In the
        rem backup above, so removing them loses nothing.
        if exist "release.bat" del "release.bat"
        if exist "settings.default.yaml" del "settings.default.yaml"
    )

    rem Dependencies can move with a release, and re-running this is cheap when
    rem they have not. install.bat is safe to repeat by design.
    echo.
    echo   Updating dependencies...
    rem Relative to this folder, which is the current one: a full path
    rem would lose any ! in it here. The .\ is there because a plain name
    rem is not looked for in the current folder when Windows is set not to
    rem (NoDefaultCurrentDirectoryInExePath).
    call .\install.bat
    rem install.bat exits 1 on every failure it reports. The code was already
    rem updated by then, so saying "Up to date" would send the operator off to
    rem start a bot whose dependencies are missing or stale.
    if errorlevel 1 (
        echo.
        echo   The code was updated, but installing its dependencies FAILED --
        echo   the reason is above. The bot may not start, or may run with the
        echo   old ones. Fix what it says, then run install.bat again.
        echo.
        pause
        exit /b 1
    )

    echo.
    echo   Up to date. Your settings and key were left alone.
    echo   If app\settings.default.yaml gained options you want, copy them across.
    echo.
    pause
    exit /b 0
)
