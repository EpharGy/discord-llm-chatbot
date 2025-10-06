@echo off
setlocal ENABLEDELAYEDEXPANSION

REM Change to the directory of this script
cd /d "%~dp0"

REM Ensure Python is available
where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found in PATH. Install Python 3.10+ and try again.
  pause
  exit /b 1
)

REM Create virtual environment if missing
if not exist .venv\Scripts\python.exe (
  echo [SETUP] Creating virtual environment (.venv)...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
  )
  echo [SETUP] Upgrading pip...
  call .venv\Scripts\python.exe -m pip install --upgrade pip wheel >nul 2>&1
  echo [SETUP] Installing dependencies...
  if exist requirements.txt (
    call .venv\Scripts\python.exe -m pip install -r requirements.txt
  ) else (
    if exist pyproject.toml (
      call .venv\Scripts\python.exe -m pip install -e .
    ) else (
      if exist setup.cfg ( call .venv\Scripts\python.exe -m pip install -e . ) else (
        if exist setup.py ( call .venv\Scripts\python.exe -m pip install -e . ) else (
          echo [WARN] No requirements.txt or project metadata found. Skipping dependency install.
        )
      )
    )
  )
)

REM Activate venv
call .venv\Scripts\activate.bat
if errorlevel 1 (
  echo [ERROR] Failed to activate virtual environment.
  pause
  exit /b 1
)

REM Set PYTHONPATH so app can import from src
set PYTHONPATH=src

echo [RUN] Starting Discord/Web bot...
python -m src.bot_app
set EXITCODE=%ERRORLEVEL%

if not %EXITCODE%==0 (
  echo [ERROR] Bot exited with code %EXITCODE%.
  pause
)

endlocal
exit /b %EXITCODE%
