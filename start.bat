@echo off
REM Start SMW Stream Tools. Keep this window open while you stream.
cd /d "%~dp0"
python run.py %*
if errorlevel 1 (
  echo.
  echo The app exited with an error. If Python is missing, install it from
  echo python.org and tick "Add python.exe to PATH" during setup.
  pause
)
