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
set "BOT_PROXY="
if exist "proxy.local" (
    for /f "usebackq tokens=* delims=" %%L in ("proxy.local") do (
        set "LINE=%%L"
        if not "!LINE!"=="" if not "!LINE:~0,1!"=="#" set "BOT_PROXY=%%L"
    )
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

rem --no-deps is required: bulk-client declares bulk-keychain, which it never
rem imports and which has no wheel for current Pythons, so pip would try to
rem build it from Rust source and fail. Its real dependencies are installed in
rem the next step.
rem Retried, because pip fetches this by cloning from GitHub and that fetch
rem fails on an unreliable route: the same address timed out after 21s and then
rem answered in 1.3s on the network this was written for. One failure is not
rem evidence of anything.
echo   Installing the BULK SDK (from GitHub -- the PyPI build cannot sign)...
set "SDK_OK="
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
if not defined SDK_OK (
    echo.
    rem Not "install git": that was checked at the top of this script, so by
    rem here it is present and the message was telling the operator to install
    rem something they already had.
    echo   Could not install the BULK SDK after three tries.
    echo.
    echo   pip fetches it from github.com. If the error above mentions a
    echo   connection or a timeout, that is the whole problem -- check your
    echo   internet and run install.bat again.
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
rem certifi is named even though requests would pull it in anyway: the bot now
rem uses it directly, to verify the WebSocket against the same trust anchors as
rem its HTTP calls. A dependency that is only there by accident is one a future
rem version of requests can drop.
rem python-socks and PySocks are what let a SOCKS proxy work -- the first for the
rem account stream, the second for HTTP. Neither library says anything useful at
rem the point of failure without them, so they are installed for everyone rather
rem than left for whoever turns out to need a proxy.
set "DEPS_OK="
for /L %%i in (1,1,3) do (
    if not defined DEPS_OK (
        if %%i GTR 1 (
            echo   That did not come through. Trying again ^(%%i of 3^)...
            ping -n 3 127.0.0.1 >nul
        )
        "%VENV_PY%" -m pip install !PIPARG! --quiet pandas numpy numba websockets pynacl base58 sortedcontainers aiohttp requests PyYAML certifi python-socks PySocks
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
    >> private_key.local echo # for by, the master -- so this one key is all the bot needs.
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
