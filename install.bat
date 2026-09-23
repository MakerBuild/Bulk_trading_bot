@echo off
rem First-time setup: builds the virtualenv and installs everything.
rem Safe to re-run; it upgrades an existing install in place.

setlocal enabledelayedexpansion
cd /d "%~dp0"

rem This file stays pure ASCII. cmd reads a .bat through the console's
rem codepage, and on a Russian Windows that is 866, where UTF-8 Cyrillic
rem desynchronises the parser -- it does not merely render wrong, it splits
rem commands apart and the install fails. So the guide is described here
rem rather than named, because its name is Cyrillic.

rem Both prerequisites are checked before anything is downloaded. Finding out
rem git is missing halfway through a 200MB install is a poor way to learn it.
where python >nul 2>&1
if errorlevel 1 (
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
set "PY_CHECK=python"
if exist "app\.venv\Scripts\python.exe" set "PY_CHECK=app\.venv\Scripts\python.exe"
set "PY_FOUND="
rem Unquoted inside the for /f on purpose: both values above are space-free,
rem relative to this folder, and a leading quote there makes cmd strip the
rem wrong pair of quotes from the command.
for /f "delims=" %%V in ('!PY_CHECK! -c "import platform; print(platform.python_version(), platform.architecture()[0])" 2^>nul') do set "PY_FOUND=%%V"
if not defined PY_FOUND set "PY_FOUND=nothing -- it did not run"
"!PY_CHECK!" -c "import sys, struct; sys.exit(0 if sys.version_info >= (3, 12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
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
    )
    echo.
    echo   Full walkthrough: the step-by-step guide in this folder
    echo.
    pause
    exit /b 1
)
"!PY_CHECK!" -c "import sys; sys.exit(1 if sys.version_info[:2] > (3, 14) else 0)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Note: Python !PY_FOUND! is newer than anything this has been tested
    echo   on. If installing the dependencies fails below, numba or llvmlite
    echo   probably has no build for it yet -- Python 3.14 is known to work.
)

if not exist "app\.venv\Scripts\python.exe" (
    echo.
    echo   Creating the virtualenv...
    python -m venv app\.venv
    if errorlevel 1 (
        echo   Could not create app\.venv -- see the error above.
        pause
        exit /b 1
    )
)

set "VENV_PY=app\.venv\Scripts\python.exe"

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
rem It is read with delayed expansion OFF. With it on, `set "X=%%L"` treats
rem every ! in the line as a variable reference and deletes it, so a password
rem like pa!ss reached the proxy as pass and every request was refused. Each
rem line is copied raw, tested in a short-lived scope where !LINE! is safe --
rem expanding a variable never re-expands its value -- and the verdict leaves
rem that scope through the for variable; the raw line is assigned only after
rem the endlocal, back where ! means nothing. The scope around the loop is
rem then left open under a fresh enabledelayedexpansion rather than closed,
rem because closing it would throw BOT_PROXY away with it. From here on the
rem value is only ever read as !BOT_PROXY!, which inserts it verbatim.
set "BOT_PROXY="
if exist "proxy.local" (
    setlocal DisableDelayedExpansion
    for /f "usebackq tokens=* delims=" %%L in ("proxy.local") do (
        set "LINE=%%L"
        setlocal EnableDelayedExpansion
        set "KEEP="
        if not "!LINE!"=="" if not "!LINE:~0,1!"=="#" set "KEEP=1"
        for %%K in ("!KEEP!") do endlocal & if "%%~K"=="1" set "BOT_PROXY=%%L"
    )
    setlocal EnableDelayedExpansion
)
set "PIPARG="
if defined BOT_PROXY (
    echo   Using the proxy from proxy.local.
    set "ALL_PROXY=!BOT_PROXY!"
    set "PIP_PROXY=!BOT_PROXY!"
    if /I "!BOT_PROXY:~0,5!"=="socks" set "PIP_PROXY=http://!BOT_PROXY:*//=!"
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
    echo   Installing the BULK SDK...
    rem --force-reinstall because the SDK's version string does not change
    rem between commits: without it pip sees 0.1.2 installed already and keeps
    rem whatever an earlier run left behind. The file is local, so repeating
    rem this costs nothing.
    "%VENV_PY%" -m pip install !PIPARG! --quiet --no-deps --force-reinstall "!SDK_WHEEL!"
    if not errorlevel 1 set "SDK_OK=1"
    if not defined SDK_OK echo   The bundled copy would not install. Trying GitHub instead.
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

rem Create the key file on first run, so there is one obvious place to put the
rem key rather than a template to notice and rename.
if not exist "private_key.local" (
    > private_key.local echo # Paste your BULK master account's base58 private key on the line
    >> private_key.local echo # below -- one line, no quotes, nothing else.
    >> private_key.local echo #
    >> private_key.local echo # This file never leaves your machine. Once the key is in, encrypt it
    >> private_key.local echo # from the menu: Accounts Management -^> Encrypt Private Key.
    >> private_key.local echo #
    >> private_key.local echo # A sub-account has no key of its own -- it is created by, and signed
    >> private_key.local echo # for by, its master. To trade several masters, put each key on a
    >> private_key.local echo # line of its own; add them all before encrypting.
    >> private_key.local echo.
    echo   Created private_key.local for your key.
)

rem Optional, and left empty on purpose: most operators never touch it. It
rem exists so that someone in a country where BULK is blocked has one obvious
rem place to put a proxy, rather than a config setting to discover.
rem Kept in step with proxy.PROXY_TEMPLATE, which test_proxy checks.
if not exist "proxy.local" (
    > proxy.local echo # Optional. Leave this file as it is unless BULK is blocked where you are.
    >> proxy.local echo #
    >> proxy.local echo # Put ONE proxy address on the line below -- no quotes, nothing else. Examples:
    >> proxy.local echo #
    >> proxy.local echo #     http://user:password@proxy.example.com:8080
    >> proxy.local echo #     socks5h://user:password@proxy.example.com:1080
    >> proxy.local echo #     socks5h://proxy.example.com:1080
    >> proxy.local echo #
    >> proxy.local echo # socks5h is the one to ask your provider for: the "h" means the proxy resolves
    >> proxy.local echo # the hostname, so the lookup does not happen on your machine -- which is what
    >> proxy.local echo # fails first where DNS is what does the blocking.
    >> proxy.local echo #
    >> proxy.local echo # Everything the bot does goes through it: orders, positions, prices and the
    >> proxy.local echo # account stream. This file never leaves your machine, but it usually holds a
    >> proxy.local echo # password, so treat it like the key file.
    echo   Created proxy.local ^(only needed if BULK is blocked where you are^).
)

rem Hide the repo's own bookkeeping, so the folder shows only the four files
rem an operator uses. git reads .gitignore regardless of the attribute, the
rem same way it hides .git itself.
if exist ".gitignore" attrib +h ".gitignore" >nul 2>&1

echo.
"%VENV_PY%" -c "from bulk_api.common import SignatureDomain; print('  Signing check: OK')" 2>nul
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
