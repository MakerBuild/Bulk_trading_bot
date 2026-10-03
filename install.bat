@echo off
rem First-time setup: builds the virtualenv and installs everything.
rem Safe to re-run; it upgrades an existing install in place.

rem The folder first, delayed expansion after. With it on, the `!` in a path
rem like C:\Users\Me!\bot is read as a variable reference and deleted, so
rem `cd` went to a folder that does not exist and every relative path after
rem it missed.
setlocal
cd /d "%~dp0"
setlocal enabledelayedexpansion

rem This file stays pure ASCII. cmd reads a .bat through the console's
rem codepage, and on a Russian Windows that is 866, where UTF-8 Cyrillic
rem desynchronises the parser -- it does not merely render wrong, it splits
rem commands apart and the install fails. So the guide is described here
rem rather than named, because its name is Cyrillic.

rem Which Python builds the virtualenv, when there is none yet. The one on
rem PATH if it is new enough; otherwise the newest 3.12+ the py launcher knows
rem of. PATH is often someone else's to decide -- other software pinned to an
rem older Python, which a global change would break -- and python.org installs
rem the launcher either way, so a 3.14 installed WITHOUT "Add to PATH" is
rem enough. Only the first build asks: once app\.venv exists, every script
rem here runs the Python inside it, whatever PATH says later.
rem
rem Kept as a command rather than resolved to a path: "py -3.14" has no
rem quotes to get wrong, and an install path under Program Files would.
set "PY_NEW=python"
if not exist "app\.venv\Scripts\python.exe" (
    call :suitable python
    if errorlevel 1 (
        for %%X in (3.14 3.13 3.12) do (
            if "!PY_NEW!"=="python" (
                call :suitable py -%%X
                if not errorlevel 1 set "PY_NEW=py -%%X"
            )
        )
    )
)

rem Both prerequisites are checked before anything is downloaded. Finding out
rem git is missing halfway through a 200MB install is a poor way to learn it.
rem PATH need not have a python when the launcher found one above.
set "PY_ON_PATH=1"
if "!PY_NEW!"=="python" (
    where python >nul 2>&1
    if errorlevel 1 set "PY_ON_PATH="
)
if not defined PY_ON_PATH (
    echo.
    echo   Python is not installed, or was installed without "Add to PATH".
    echo.
    echo   Get it from https://www.python.org/downloads/ and TICK THE BOX
    echo   "Add python.exe to PATH" at the bottom of the installer.
    echo.
    echo   Full walkthrough: the step-by-step guide in this folder
    echo.
    pause
    exit /b 1
)

where git >nul 2>&1
if errorlevel 1 (
    echo.
    echo   git is not installed. It is needed to fetch the BULK library.
    echo.
    echo   Get it from https://git-scm.com/downloads and keep every default.
    echo   Then CLOSE THIS WINDOW and run install.bat again -- an open console
    echo   does not pick up a newly installed program.
    echo.
    echo   Full walkthrough: the step-by-step guide in this folder
    echo.
    pause
    exit /b 1
)

rem The pinned dependencies in app\docs\requirements.txt need a 64-bit Python
rem 3.12 or newer: numpy 2.5 is the floor, and numba and llvmlite publish no
rem 32-bit Windows wheels. The SDK wheel itself asks only for 3.10, and the
rem bulk-keychain it declares -- the package with no wheels for new Pythons --
rem is never installed, because the SDK goes in with --no-deps. So these are
rem the real limits, and 3.12 through 3.14 is what has been run.
rem
rem Checked here because the failure otherwise comes from inside pip, as a
rem wall of build errors for numpy or llvmlite, and the message after it said
rem to check the internet. Checked against the interpreter that will run the
rem bot: the existing virtualenv if there is one, since a re-run keeps it.
set "PY_CHECK=!PY_NEW!"
if exist "app\.venv\Scripts\python.exe" set "PY_CHECK=app\.venv\Scripts\python.exe"
set "PY_FOUND="
rem Unquoted, here and below, on purpose: every value above is space-free
rem apart from the one between "py" and its version, which has to split. A
rem leading quote in the for /f makes cmd strip the wrong pair of quotes, and
rem quoting "py -3.14" would ask for a program by that whole name.
for /f "delims=" %%V in ('!PY_CHECK! -c "import platform; print(platform.python_version(), platform.architecture()[0])" 2^>nul') do set "PY_FOUND=%%V"
if not defined PY_FOUND set "PY_FOUND=nothing -- it did not run"
call !PY_CHECK! -c "import sys, struct; sys.exit(0 if sys.version_info >= (3, 12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   This needs 64-bit Python 3.12 or newer. Found: !PY_FOUND!
    echo.
    if exist "app\.venv\Scripts\python.exe" (
        echo   That is the Python app\.venv was built with. Install a newer one
        echo   from https://www.python.org/downloads/ and tick the box
        echo   "Add python.exe to PATH", then delete the app\.venv folder and
        echo   run install.bat again. Your settings and key are not in it.
    ) else (
        echo   Get it from https://www.python.org/downloads/ and TICK THE BOX
        echo   "Add python.exe to PATH" at the bottom of the installer. If it
        echo   says it did not run, the python on PATH is usually the Microsoft
        echo   Store placeholder, which the real install replaces.
        echo.
        echo   If other software needs the older Python on PATH, leave that box
        echo   UNTICKED instead: this finds a 3.12+ through the py launcher,
        echo   and uses it for this folder only.
    )
    echo.
    echo   Full walkthrough: the step-by-step guide in this folder
    echo.
    pause
    exit /b 1
)
call !PY_CHECK! -c "import sys; sys.exit(1 if sys.version_info[:2] > (3, 14) else 0)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Note: Python !PY_FOUND! is newer than anything this has been tested
    echo   on. If installing the dependencies fails below, numba or llvmlite
    echo   probably has no build for it yet -- Python 3.14 is known to work.
)

rem Refused while the bot is running from this folder: the steps below
rem reinstall the libraries it has loaded. The check is bulkdn\lock.py, which
rem needs nothing installed; a Python that cannot run it exits 1, not 3.
if exist "app\.venv\Scripts\python.exe" (
    set "PYTHONPATH=app"
    app\.venv\Scripts\python.exe -m bulkdn.lock
    if errorlevel 3 (
        echo.
        echo   Stop the bot first, then run this again. Nothing was changed.
        echo.
        pause
        exit /b 1
    )
)

if not exist "app\.venv\Scripts\python.exe" (
    echo.
    echo   Creating the virtualenv...
    echo   ^(with !PY_NEW!^)
    call !PY_NEW! -m venv app\.venv
    if errorlevel 1 (
        rem A failed attempt can leave a half-built folder, which the next
        rem run would take for a finished one and skip this step.
        if exist "app\.venv" rd /s /q "app\.venv"
        echo   Could not create app\.venv -- see the error above.
        pause
        exit /b 1
    )
)

set "VENV_PY=app\.venv\Scripts\python.exe"
rem The bot's own package, for the questions below that this script asks it
rem rather than answering itself: which proxy, whether the SDK is current,
rem the key and proxy templates, the signing check.
set "PYTHONPATH=app"

echo.
rem The bot already reads proxy.local, because BULK is unreachable from some
rem countries. GitHub and PyPI are unreachable from the same ones, and an
rem install that cannot fetch is as stuck as a bot that cannot trade.
rem
rem Two proxies, because the two tools disagree about what they accept:
rem
rem   git  works through SOCKS and aborts the CONNECT on an HTTP proxy, so it
rem        gets the line as written, through ALL_PROXY.
rem   pip  raises "PoolKey.__new__() got an unexpected keyword argument
rem        key_proxy_ssl_context" on any socks address -- a fault in its own
rem        vendored urllib3 -- so it gets the same host and port with an http
rem        scheme, passed as --proxy rather than through the environment.
rem
rem Both verified end to end against a network where github.com is blocked.
rem The line is never echoed: it usually carries a password.
rem
rem The line comes from the bot's own reader, `python -m bulkdn.scripts
rem proxy`, so git, pip and the bot all use the same address. This script used
rem to parse the file itself and took the LAST usable line, where the shell
rem scripts took the first and the bot refused a file with two. It is read
rem with delayed expansion OFF: with it on, `set "X=%%L"` treats every ! in the
rem line as a variable reference and deletes it, so a password like pa!ss
rem reached the proxy as pass. The scope around the reads is then left open
rem under a fresh enabledelayedexpansion rather than closed, because closing
rem it would throw BOT_PROXY away with it. From here on the value is only ever
rem read as !BOT_PROXY!, which inserts it verbatim.
set "BOT_PROXY="
set "PIP_PROXY="
if exist "proxy.local" (
    %VENV_PY% -m bulkdn.scripts proxy >nul
    if errorlevel 1 (
        echo.
        echo   proxy.local cannot be used -- the reason is above. Fix it, or
        echo   empty it, and run install.bat again.
        echo.
        pause
        exit /b 1
    )
    setlocal DisableDelayedExpansion
    for /f "usebackq delims=" %%L in (`%VENV_PY% -m bulkdn.scripts proxy 2^>nul`) do set "BOT_PROXY=%%L"
    for /f "usebackq delims=" %%L in (`%VENV_PY% -m bulkdn.scripts pip-proxy 2^>nul`) do set "PIP_PROXY=%%L"
    setlocal EnableDelayedExpansion
)
set "PIPARG="
if defined BOT_PROXY (
    echo   Using the proxy from proxy.local.
    set "ALL_PROXY=!BOT_PROXY!"
    set "PIPARG=--proxy !PIP_PROXY!"
)

echo   Updating pip...
"%VENV_PY%" -m pip install !PIPARG! --quiet --upgrade pip
rem Not fatal. The pip a 3.12+ virtualenv starts with installs every wheel
rem below; the upgrade only saves a nag. A failure here is nearly always the
rem network, and the next step will say so again if it is -- stopping here
rem would be stopping for the lesser of the two.
if errorlevel 1 (
    echo   Could not update pip. Carrying on with the one the virtualenv came
    echo   with, which is normally fine. If the steps below fail as well, the
    echo   error above is the likely reason.
)

rem --no-deps is required: bulk-client declares bulk-keychain, which it never
rem imports and which has no wheel for current Pythons, so pip would try to
rem build it from Rust source and fail. Its real dependencies are installed in
rem the next step.
rem
rem The SDK ships with the bot, in app\vendor. It is the only part of this
rem install that does not come from PyPI, and github.com is unreachable from
rem some of the networks this is handed out on: measured here, three pip clones
rem of it failed at twenty-one seconds each while PyPI answered throughout. A
rem new operator meets that failure before anything has worked once, and reads
rem it as a broken bot rather than a bad route.
rem
rem The filename is pinned, like the commit below it. app\vendor\README.md says
rem what has to move when the SDK does.
set "SDK_WHEEL=app\vendor\bulk_client-0.1.2-py3-none-any.whl"
set "SDK_OK="
if exist "!SDK_WHEEL!" (
    rem Only when it changed. The SDK's version string does not change
    rem between commits, so pip would keep whatever an earlier run left behind
    rem -- hence --force-reinstall below -- but forcing it on every run also
    rem replaced the SDK under a bot running from it, during every update. The
    rem wheel's hash is recorded in app\.venv after a successful install and
    rem compared here instead.
    %VENV_PY% -m bulkdn.scripts sdk-current "!SDK_WHEEL!" >nul 2>&1
    if not errorlevel 1 (
        echo   The BULK SDK is up to date.
        set "SDK_OK=1"
    ) else (
        echo   Installing the BULK SDK...
        "%VENV_PY%" -m pip install !PIPARG! --quiet --no-deps --force-reinstall "!SDK_WHEEL!"
        if not errorlevel 1 (
            set "SDK_OK=1"
            %VENV_PY% -m bulkdn.scripts sdk-stamp "!SDK_WHEEL!"
        )
        if not defined SDK_OK echo   The bundled copy would not install. Trying GitHub instead.
    )
)

rem Fallback for a copy that predates the wheel, which has no app\vendor at
rem all. Retried, because pip fetches this by cloning from GitHub and that
rem fetch fails on an unreliable route: the same address timed out after 21s
rem and then answered in 1.3s on the network this was written for. One failure
rem is not evidence of anything.
if not defined SDK_OK (
    echo   Installing the BULK SDK ^(from GitHub -- the PyPI build cannot sign^)...
    for /L %%i in (1,1,3) do (
        if not defined SDK_OK (
            if %%i GTR 1 (
                echo   That did not come through. Trying again ^(%%i of 3^)...
                ping -n 3 127.0.0.1 >nul
            )
            "%VENV_PY%" -m pip install !PIPARG! --quiet --no-deps "bulk-client @ git+https://github.com/Bulk-trade/bulk-client.git@3a6506e#subdirectory=crates/api-python"
            if not errorlevel 1 set "SDK_OK=1"
        )
    )
)
if not defined SDK_OK (
    echo.
    rem Not "install git": that was checked at the top of this script, so by
    rem here it is present and the message was telling the operator to install
    rem something they already had.
    echo   Could not install the BULK SDK.
    echo.
    echo   A copy ships with the bot, in app\vendor. If it is missing, this
    echo   falls back to fetching it from github.com -- and an error above
    echo   about a connection or a timeout is then the whole problem. Check
    echo   your internet and run install.bat again.
    echo.
    pause
    exit /b 1
)

rem pip checks consistency after this step and reports bulk-keychain and
rem solders as missing, in red, with the word ERROR. Both are declared by
rem bulk-client and imported by nothing in it -- checked against the installed
rem package -- so the message is noise. Said here rather than hidden, because
rem suppressing pip's output would hide a real failure too.
echo   Installing dependencies...
echo   ^(pip will report bulk-keychain and solders as missing. That is expected:
echo    the SDK declares them and never imports them. The signing check at the
echo    end is what tells you the install works.^)
rem The list, pinned, lives in app\docs\requirements.txt and nowhere else. It
rem used to be written out here as well, unpinned, and the two drifted apart.
rem That file says why each entry is there -- certifi, python-socks and PySocks
rem included -- and why the SDK, installed above, is not.
set "DEPS_OK="
for /L %%i in (1,1,3) do (
    if not defined DEPS_OK (
        if %%i GTR 1 (
            echo   That did not come through. Trying again ^(%%i of 3^)...
            ping -n 3 127.0.0.1 >nul
        )
        "%VENV_PY%" -m pip install !PIPARG! --quiet -r "app\docs\requirements.txt"
        if not errorlevel 1 set "DEPS_OK=1"
    )
)
if not defined DEPS_OK (
    echo.
    echo   Could not install the dependencies after three tries -- see the
    echo   error above. A connection or timeout message there means the
    echo   download failed; run install.bat again.
    echo.
    pause
    exit /b 1
)

rem The bot is started as `python -m bulkdn` from this directory, so the
rem package is already importable and needs no install of its own. Skipping it
rem also keeps a bulkdn.egg-info folder out of the way.

rem The operator's settings are a copy of the shipped defaults, not the shipped
rem file itself. Keeping them apart is what lets an update be a `git pull`:
rem a tracked settings.yaml would conflict for anyone who had changed a size.
if not exist "settings.yaml" (
    copy /y "app\settings.default.yaml" "settings.yaml" >nul
    echo   Created settings.yaml from the defaults -- edit that one.
)

rem The key file, on first run, so there is one obvious place to put the key
rem rather than a template to notice and rename. And proxy.local, empty on
rem purpose: most operators never touch it, but someone where BULK is blocked
rem needs one obvious place to put a proxy. Both are written from the texts the
rem bot itself uses -- this script used to echo its own copy of each, line by
rem line, and a test had to check the two had not drifted.
%VENV_PY% -m bulkdn.scripts templates
if errorlevel 1 (
    echo   Could not write the key and proxy templates -- see the error above.
    pause
    exit /b 1
)

rem Hide the repo's own bookkeeping, so the folder shows only the four files
rem an operator uses. git reads .gitignore regardless of the attribute, the
rem same way it hides .git itself.
if exist ".gitignore" attrib +h ".gitignore" >nul 2>&1

echo.
rem A real check: a transaction for a throwaway key is signed by the SDK, its
rem signature-domain byte checked -- the PyPI build leaves it off and the
rem exchange refuses everything it signs -- and the signature verified. This
rem used to import one name and call that OK.
%VENV_PY% -m bulkdn.scripts signing-check
if errorlevel 1 (
    echo   WARNING: the SDK cannot sign. Do not trade with this install.
    pause
    exit /b 1
)

echo.
echo   Done. Next:
echo     1. Put your base58 private key on one line in private_key.local
echo     2. Edit settings.yaml -- the only settings file you touch
echo     3. Run run.bat
echo.
pause
rem Explicit, because update.bat calls this and reads the exit code to decide
rem whether to say "Up to date".
exit /b 0


rem Whether the command given runs a 64-bit Python 3.12 or newer. Called
rem with the command split into words -- "python", or "py -3.14".
rem
rem `call` here and at every other run of a Python below: a python that is
rem a .bat -- pyenv-win's shims are -- would otherwise end this script
rem there, since cmd does not come back from a batch file run without it.
:suitable
call %* -c "import sys, struct; sys.exit(0 if sys.version_info >= (3, 12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
exit /b %errorlevel%
