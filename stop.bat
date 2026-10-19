@echo off
setlocal
echo Stopping IBVAP services...
taskkill /F /FI "WINDOWTITLE eq IBVAP-Backend*" /T >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq IBVAP-Frontend*" /T >nul 2>&1
echo IBVAP services stopped.
