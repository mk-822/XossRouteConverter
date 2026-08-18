@echo off
setlocal
cd /d "%~dp0"
if not exist VERSION (
  echo VERSION file not found.
  pause
  exit /b 1
)
set /p VERSION=<VERSION
if "%VERSION%"=="" (
  echo VERSION file is empty.
  pause
  exit /b 1
)
set "APP_NAME=XOSS_NAV_Route_Converter-v%VERSION%"
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "%APP_NAME%" --add-data "assets\xoss_nav_template.ro;assets" --add-data "VERSION;." xoss_route_converter.py
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)
echo Built: dist\%APP_NAME%.exe
pause
