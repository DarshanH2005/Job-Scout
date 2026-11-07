@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if not errorlevel 1 (
  uv run --no-project --with-requirements "%~dp0requirements.txt" python -m scout.launch
  goto finish
)
where py >nul 2>nul
if errorlevel 1 (
  echo Install Python 3.11 or newer, then launch again.
  pause
  exit /b 1
)
set "SCOUT_VENV=%LOCALAPPDATA%\JobScout\venv"
if not exist "%SCOUT_VENV%\Scripts\python.exe" (
  py -3 -m venv "%SCOUT_VENV%"
  if errorlevel 1 goto finish
)
"%SCOUT_VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r "%~dp0requirements.txt"
if errorlevel 1 goto finish
"%SCOUT_VENV%\Scripts\python.exe" -m scout.launch
:finish
if errorlevel 1 pause
