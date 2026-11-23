@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Starcarft Arena - Local GUI
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [Arena] Python virtual environment missing. Installing local environment...
  py -3.12 -m venv ".venv"
  if errorlevel 1 goto :failed
  "%PY%" -m pip install -r "requirements.txt"
  if errorlevel 1 goto :failed
)
"%PY%" -c "import sc2" >nul 2>&1
if errorlevel 1 (
  echo [Arena] Installing project dependencies in .venv...
  "%PY%" -m pip install -r "requirements.txt"
  if errorlevel 1 goto :failed
)
echo.
echo    Starcarft Arena - http://127.0.0.1:8765
echo    Close this console / Ctrl+C to shut down the GUI service.
echo    A game launches ONLY after clicking Start in the GUI.
echo.
"%PY%" -u "launcher\server.py"
if errorlevel 1 goto :failed
goto :done
:failed
echo.
echo [Arena] Startup failed. Review the error above.
pause
:done
endlocal
