@echo off
setlocal
cd /d "%~dp0"

rem -- Python 3.10+ on PATH? (also catches the Microsoft Store "python" stub)
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer was not found on PATH.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    goto fail
)

rem -- a .venv copied from another PC points at that PC's Python: rebuild it
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -c "import sys" >nul 2>nul
    if errorlevel 1 (
        echo The existing .venv does not work on this PC - recreating it...
        rmdir /s /q .venv
    )
)
if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto fail
)

echo Installing / checking dependencies...
.venv\Scripts\python.exe -m pip install -q -e .
if errorlevel 1 goto fail

.venv\Scripts\python.exe -m dataflow.cli
echo.
echo Data Flow stopped.
goto end

:fail
echo.
echo Data Flow could not start - see the message above.

:end
pause
