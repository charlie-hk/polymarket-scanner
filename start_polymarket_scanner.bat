@echo off
cd /d "%~dp0"
title Polymarket Scanner
echo Polymarket scanner is running. Close this window or press Ctrl+C to stop.
:loop
python polymarket_scanner.py --loop 120
echo.
echo Scanner stopped unexpectedly. Restarting in 60 seconds...
timeout /t 60 /nobreak >nul
goto loop
