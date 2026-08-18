@echo off
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed --name XOSS_NAV_Route_Converter --add-data "assets\xoss_nav_template.ro;assets" xoss_route_converter.py
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)
echo Built: dist\XOSS_NAV_Route_Converter.exe
pause
