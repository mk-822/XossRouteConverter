@echo off
cd /d "%~dp0"
py -3 xoss_route_converter.py
if errorlevel 1 pause
