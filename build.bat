@echo off
REM Build a single-file Windows executable and leave it in this folder.
cd /d "%~dp0"
setlocal

set EXENAME=SMW Stream Tools

REM The tray needs pystray and Pillow to be installed at BUILD time, or
REM PyInstaller has nothing to bundle and the exe silently has no icon.
echo Checking dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 goto fail

REM Invoke through "python -m": the pyinstaller script shim often isn't on
REM PATH even when the package is installed.
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo Installing PyInstaller...
  python -m pip install pyinstaller
  if errorlevel 1 (
    echo.
    echo Could not install PyInstaller. If pip needs admin rights, try:
    echo     python -m pip install --user pyinstaller
    goto fail
  )
)

echo.
echo Building...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "%EXENAME%" ^
  --add-data "smwtools/static;smwtools/static" ^
  --hidden-import pystray._win32 ^
  --hidden-import PIL._tkinter_finder ^
  run.py
if errorlevel 1 goto fail

if not exist "dist\%EXENAME%.exe" (
  echo Build reported success but dist\%EXENAME%.exe is missing.
  goto fail
)

REM Put the exe in this folder, so its "data" folder lands here too rather
REM than inside dist.
if exist "%EXENAME%.exe" del /q "%EXENAME%.exe"
move /y "dist\%EXENAME%.exe" "%EXENAME%.exe" >nul
if errorlevel 1 goto fail

rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q "%EXENAME%.spec" 2>nul

echo.
echo   Built: %EXENAME%.exe
echo.
echo   Settings go in the "data" folder beside it, so keep that folder when
echo   you replace the exe. If it does not start, look for startup-error.txt.
goto end

:fail
echo.
echo   BUILD FAILED - see the messages above.

:end
echo.
pause
