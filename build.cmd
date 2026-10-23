@echo off
rem ============================================================
rem  Build script for D2TradeHelper.exe (PyInstaller)
rem  Creates a single-file, windowless exe for distribution.
rem ============================================================
cd /d "%~dp0"

rem --- Configuration ------------------------------------------
set "ENTRY=main.py"
set "ICON=Logo.ico"
set "APP_NAME=D2TradeHelper v0.2"
set "PYINSTALLER=python -m PyInstaller"

rem --- UPX (compression) --------------------------------------
set "UPX_DIR=C:\upx-5.2.1-win64"

rem --- Data files to bundle (source;dest) ---------------------
set "DATA_FILES=--add-data "tooltip_best_v6.pt;." --add-data "Logo.ico;." --add-data "Logo.png;." --add-data "Prompt.txt;.""

rem --- Packages that need full collection ---------------------
set "COLLECT_ALL=--collect-all ultralytics --collect-all torchvision --collect-all torch --collect-all pystray --collect-all PIL --collect-all customtkinter --collect-all cv2 --collect-all mss --collect-all pynput --collect-all reportlab --collect-all pygetwindow --collect-all numpy"

rem --- Modules to exclude (shrink archive, faster extraction) ---
set "EXCLUDES="

rem --- Clean previous artifacts -------------------------------
echo [build] Cleaning previous artifacts...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

rem --- Build --------------------------------------------------
echo [build] Running PyInstaller...
%PYINSTALLER% ^
  --noconfirm --clean --onefile --noconsole ^
  --upx-dir "%UPX_DIR%" ^
  --splash "Logo.png" ^
  --icon "%ICON%" ^
  --name "%APP_NAME%" ^
  %DATA_FILES% ^
  %COLLECT_ALL% ^
  %EXCLUDES% ^
  "%ENTRY%"

if %ERRORLEVEL% neq 0 (
  echo [build] BUILD FAILED with error code %ERRORLEVEL%
  exit /b %ERRORLEVEL%
)

echo [build] BUILD SUCCESSFUL
echo [build] Output: dist\%APP_NAME%.exe
echo [build] Note: dist/ and build/ are gitignored - not committed.
pause