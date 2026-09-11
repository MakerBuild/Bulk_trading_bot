@echo off
rem First-time setup: builds the virtualenv and installs everything.
rem Safe to re-run; it upgrades an existing install in place.

setlocal
cd /d "%~dp0"

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
    echo   Full walkthrough: app\docs\INSTALL.md
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
    echo   Full walkthrough: app\docs\INSTALL.md
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
echo   Updating pip...
"%VENV_PY%" -m pip install --quiet --upgrade pip

rem --no-deps is required: bulk-client declares bulk-keychain, which it never
rem imports and which has no wheel for current Pythons, so pip would try to
rem build it from Rust source and fail. Its real dependencies are installed in
rem the next step.
echo   Installing the BULK SDK (from GitHub -- the PyPI build cannot sign)...
"%VENV_PY%" -m pip install --quiet --no-deps "bulk-client @ git+https://github.com/Bulk-trade/bulk-client.git@3a6506e#subdirectory=crates/api-python"
if errorlevel 1 (
    echo.
    echo   SDK install failed. It needs git on PATH -- https://git-scm.com/downloads
    pause
    exit /b 1
)

echo   Installing dependencies...
"%VENV_PY%" -m pip install --quiet pandas numpy numba websockets pynacl base58 sortedcontainers aiohttp requests PyYAML
if errorlevel 1 (
    echo   Dependency install failed -- see the error above.
    pause
    exit /b 1
)

rem The bot is started as `python -m bulkdn` from this directory, so the
rem package is already importable and needs no install of its own. Skipping it
rem also keeps a bulkdn.egg-info folder out of the way.

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
echo     2. Edit settings.yaml
echo     3. Run run.bat
echo.
pause
