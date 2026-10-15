@echo off
rem ============================================================
rem  qk course-grabber -- double-click launcher for the GUI
rem
rem  Why this file exists: so you do not have to remember the
rem  command or worry about which directory you are in. It cd's
rem  to its own folder and runs:  python -m qk gui
rem
rem  Arguments are passed through, for example:
rem      start-gui.bat --mock                     offline demo
rem      start-gui.bat --config my-config.yaml    custom config
rem
rem  NOTE: this file is intentionally ASCII-only. Non-ASCII text
rem  in a .bat is read using the console codepage and can corrupt
rem  cmd's parser (Chinese rem/echo lines were executed as
rem  commands when this file was GBK-encoded). Keep it ASCII.
rem ============================================================
setlocal

rem %~dp0 includes a trailing backslash; cd here so `python -m qk` finds the package
cd /d "%~dp0"

echo(
echo   qk GUI launcher - starting the window...
echo(

rem Prefer the py launcher, fall back to python
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PY=py"
) else (
    set "PY=python"
)

%PY% --version >nul 2>nul
if errorlevel 1 (
    echo   [ERROR] Python not found. Install Python 3.10+ and add it to PATH.
    echo           During setup, tick "Add Python to PATH".
    echo(
    pause
    exit /b 1
)

rem Probe PyQt6 without opening a window
%PY% -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo   [NOTE] The GUI needs the optional dependency PyQt6, not installed yet.
    echo          Installing it now ^(one time only^)...
    echo(
    %PY% -m pip install --user -r requirements-gui.txt
    if errorlevel 1 (
        echo(
        echo   [ERROR] PyQt6 install failed. Retry manually:
        echo              %PY% -m pip install --user PyQt6
        echo           The command line tools ^(run/login/list/grab/session^)
        echo           do NOT need PyQt6 and keep working without it.
        echo(
        pause
        exit /b 1
    )
    echo(
    echo   PyQt6 installed.
    echo(
)

rem Force the real Windows platform plugin. If QT_QPA_PLATFORM is inherited as
rem "offscreen" (e.g. from an automation shell), Qt renders into an invisible
rem buffer: the process starts fine, MainWindowHandle exists, but NO window is
rem ever shown. That is a genuinely confusing failure, so override it here --
rem but respect an explicit choice if the caller already set it deliberately.
if not defined QT_QPA_PLATFORM (
    set "QT_QPA_PLATFORM=windows"
    echo   [note] QT_QPA_PLATFORM was not set; forcing "windows" so the window is visible.
) else (
    echo   [note] keeping inherited QT_QPA_PLATFORM=%QT_QPA_PLATFORM%
    if /I not "%QT_QPA_PLATFORM%"=="windows" (
        echo   [warn] that is NOT "windows" -- the window may be invisible!
        echo          If you cannot see the window, delete that variable and rerun.
    )
)

rem Launch the window. This line blocks until the window is closed.
%PY% -m qk gui %*

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo(
    echo   [ERROR] GUI exited with code %RC%.
    echo           Usual causes: a bad config file, or a wrong working directory.
    echo           Try the offline demo to isolate it:  start-gui.bat --mock
    echo(
    pause
)

endlocal
exit /b %RC%