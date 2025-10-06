@echo off
REM Single-file launcher: open persistent console, ensure venv, run bot, always pause at end.

REM Relaunch in a persistent console window if not already spawned
if "%~1"=="_spawn" goto spawned
start "Discord LLM Bot" cmd /k call "%~f0" _spawn
goto end

:spawned
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 goto no_python

if exist ".venv\Scripts\python.exe" goto venv_exists
echo Creating virtual environment (.venv)...
python -m venv .venv

:venv_exists
call ".venv\Scripts\activate.bat"
set PYTHONPATH=src
echo Starting Discord/Web bot... (Ctrl+C to stop)
echo.
python -m src.bot_app
echo.
echo Press any key to close this window.
pause >nul
goto end

:no_python
echo Python not found in PATH. Install Python 3.10+ and try again.
echo.
pause

:end
exit /b
