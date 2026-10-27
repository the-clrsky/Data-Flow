"""Build dist/data-flow-<version>-windows.zip: the app with its own Python,
for people who don't have Python. They unzip it and double-click
"Data Flow.bat"; nothing is installed and no internet is needed.

The bundled Python is the Windows "embeddable" build of the version running
this script, so the dependencies pip installs here are the right ones for it.

    .venv\\Scripts\\python.exe build_package.py

Settings (data-flow.db) are never packed: they hold connection passwords.
Each copy keeps its own next to "Data Flow.bat"; see READ-ME-FIRST.txt.
"""
import os
import platform
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
VERSION = tomllib.load(open(os.path.join(ROOT, "pyproject.toml"), "rb"))["project"]["version"]
NAME = f"data-flow-{VERSION}"
STAGE = os.path.join(DIST, NAME)
PY = platform.python_version()
TAG = f"python{sys.version_info.major}{sys.version_info.minor}"

LAUNCHER = r"""@echo off
setlocal
cd /d "%~dp0"
if "%DATAFLOW_PORT%"=="" set DATAFLOW_PORT=8000
rem open the browser once the server has had a moment to start
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:%DATAFLOW_PORT%"
python\python.exe -m dataflow.cli
echo.
echo Data Flow stopped. If it closed straight away, port %DATAFLOW_PORT% may be in use:
echo another copy of Data Flow is probably still running.
pause
"""

README = f"""Data Flow {VERSION} - Oracle to PostgreSQL migration
=====================================================

START
  Double-click "Data Flow.bat". A browser tab opens at http://localhost:8000
  (if it doesn't, open that address yourself). Keep the black window open
  while you use it; close it to stop Data Flow.
  Nothing else to install: Python is included in the "python" folder.

LOAD YOUR SAVED SETTINGS (connections, mappings, dictionaries, lookups, groups)
  Settings live in data-flow.db, next to "Data Flow.bat". This zip has none.
  - From an older copy of Data Flow on this PC: stop it, then copy its
    data-flow.db into this folder before starting. Or, after starting,
    click "Import saved settings..." on the Connections tab and pick that file.
  - From another PC: there, open History > "Export all data" and send the
    .db file. Here, click "Import saved settings..." on the Connections tab
    (or History > "Import data...") and pick it.
  Importing replaces everything here; the previous data is kept as a
  data-flow.db.bak-<date> file in this folder.

  The .db file includes connection passwords. Share it only with people who
  may use those databases.

UPGRADE
  Unzip the new version over this folder (replace the files). data-flow.db
  isn't in the zip, so your settings stay.

OTHER PORT / OTHER PCs ON THE NETWORK
  Port: before starting, run  set DATAFLOW_PORT=8080  in a command prompt in
  this folder, then  "Data Flow.bat"  from the same prompt.
  Other PCs can open http://<this-pc-name-or-ip>:8000 while it runs.

Instructions for Postgres lookups: docs\\Postgres_Lookups_Instructions.docx
"""


def run(*args):
    print(">", " ".join(args))
    subprocess.run(args, check=True)


def main():
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        sys.exit("Build this on 64-bit Windows: the package carries a Windows Python.")
    if os.path.exists(STAGE):
        shutil.rmtree(STAGE)
    os.makedirs(STAGE)

    # 1. Python itself
    embed = os.path.join(DIST, f"python-{PY}-embed-amd64.zip")
    if not os.path.exists(embed):
        url = f"https://www.python.org/ftp/python/{PY}/python-{PY}-embed-amd64.zip"
        print("downloading", url)
        urllib.request.urlretrieve(url, embed)
    pydir = os.path.join(STAGE, "python")
    with zipfile.ZipFile(embed) as z:
        z.extractall(pydir)
    # the embeddable build ignores PYTHONPATH and site-packages unless its ._pth lists them
    with open(os.path.join(pydir, f"{TAG}._pth"), "w") as f:
        f.write(f"{TAG}.zip\n.\nLib\\site-packages\nimport site\n")

    # 2. the app and its dependencies, into that Python
    run(sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
        "--no-warn-script-location", "--target", os.path.join(pydir, "Lib", "site-packages"), ROOT)

    # 3. launcher and instructions
    with open(os.path.join(STAGE, "Data Flow.bat"), "w", newline="\r\n") as f:
        f.write(LAUNCHER)
    with open(os.path.join(STAGE, "READ-ME-FIRST.txt"), "w", newline="\r\n", encoding="utf-8") as f:
        f.write(README)
    for doc in ("LICENSE", "docs"):
        src = os.path.join(ROOT, doc)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(STAGE, doc))
        elif os.path.exists(src):
            shutil.copy(src, STAGE)

    # nobody's settings or passwords go out in the zip
    leaked = [os.path.join(d, f) for d, _, fs in os.walk(STAGE) for f in fs
              if f.endswith(".db") or ".db.bak" in f]
    if leaked:
        sys.exit("Refusing to package settings files: " + ", ".join(leaked))

    out = shutil.make_archive(os.path.join(DIST, f"{NAME}-windows"), "zip", DIST, NAME)
    print(f"\nbuilt {out} ({os.path.getsize(out) / 2**20:.0f} MB)")


if __name__ == "__main__":
    main()
