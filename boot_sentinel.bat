@echo off
:: =====================================================================
:: Sentinel Home Server Booter
::
:: Starts the Mosquitto broker, the AI ingestion pipeline and the Dash
:: dashboard, each in its own visible console, and runs pre-flight checks
:: first so a broken environment is reported instead of silently failing.
:: =====================================================================
setlocal enabledelayedexpansion
title Sentinel Booter

set "SENTINEL_ROOT=%~dp0"
if "%SENTINEL_ROOT:~-1%"=="\" set "SENTINEL_ROOT=%SENTINEL_ROOT:~0,-1%"
set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"
set "CONDA_ENV=dash_env"
set "MOSQUITTO_EXE=C:\Program Files\mosquitto\mosquitto.exe"
:: The TLS certificate is issued to audiosense.hmu.gr, so open the dashboard by
:: that name: the browser gets a hostname-matching certificate, and it avoids
:: any stale per-site zoom saved against "localhost".
set "DASHBOARD_URL=https://audiosense.hmu.gr:8050"

cd /d "%SENTINEL_ROOT%"

echo.
echo =====================================================================
echo  SENTINEL HOME SERVER
echo  Root: %SENTINEL_ROOT%
echo =====================================================================
echo.

:: ---------------------------------------------------------------------
:: 1. Free the broker ports
:: ---------------------------------------------------------------------
echo [BROKER] Checking the MQTT broker...
netstat -ano -p tcp | findstr /r /c:":8883 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo       A broker is already listening on 8883 - reusing it.
    goto broker_ready
)

net session >nul 2>&1
if errorlevel 1 (
    echo       Not running as Administrator - cannot stop a Windows service.
    echo       If port 8883 is already taken, re-run this script as Administrator.
) else (
    net stop mosquitto >nul 2>&1
    if errorlevel 1 (
        echo       No background Mosquitto service was running.
    ) else (
        echo       Stopped the background Mosquitto service.
    )
)

:: ---------------------------------------------------------------------
:: 2. Broker
:: ---------------------------------------------------------------------
echo [BROKER] Starting Mosquitto broker (1883 local, 8883 mTLS)...
if not exist "%MOSQUITTO_EXE%" (
    echo       [FATAL] Mosquitto not found at "%MOSQUITTO_EXE%".
    echo               Sentinels will not be able to publish audio.
    pause
    exit /b 1
)
start "Sentinel - MQTT Broker" "%MOSQUITTO_EXE%" -c "%SENTINEL_ROOT%\mosquitto.conf" -v
call :sleep 3

:broker_ready

:: ---------------------------------------------------------------------
:: 3. Pre-flight checks (in this window, so the report is readable)
:: ---------------------------------------------------------------------
echo [CHECKS] Running pre-flight checks...
echo.
call "%CONDA_BAT%" activate %CONDA_ENV%
if errorlevel 1 (
    echo       [FATAL] Could not activate conda environment "%CONDA_ENV%".
    echo               Expected conda at: %CONDA_BAT%
    pause
    exit /b 1
)
python -u "%SENTINEL_ROOT%\boot\preflight.py"
set "PREFLIGHT=%ERRORLEVEL%"
if not "%PREFLIGHT%"=="0" (
    echo.
    echo  One or more checks FAILED. Starting anyway would most likely end in a
    echo  service window closing by itself. Review the list above.
    echo.
    choice /c YN /m "Continue anyway"
    if errorlevel 2 exit /b 1
)

:: ---------------------------------------------------------------------
:: 4. Services, each supervised in its own visible console
:: ---------------------------------------------------------------------
echo.
:: A dashboard left over from a previous run keeps port 8050, so the new one
:: would fail to bind while the wait loop below sees the OLD instance answering.
netstat -ano -p tcp | findstr /r /c:":8050 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [START ] Port 8050 is already in use - a dashboard from an earlier run
    echo          is probably still open. Close its window first, otherwise the
    echo          browser will show the stale instance.
    echo.
    choice /c YN /m "Start anyway"
    if errorlevel 2 exit /b 1
)

echo [START ] Starting services...
start "Sentinel - AI Pipeline" cmd /k ""%SENTINEL_ROOT%\boot\run_service.bat" "AI Pipeline" "ast_src\main.py""
call :sleep 2
start "Sentinel - Dashboard" cmd /k ""%SENTINEL_ROOT%\boot\run_service.bat" "Dashboard" "ast_dash\ast_app.py""
call :sleep 2
start "Sentinel - Mobile API" cmd /k ""%SENTINEL_ROOT%\boot\run_service.bat" "Mobile API" "mobile_api\main.py""

echo          Waiting for the dashboard to come up...
set /a WAITED=0
:waitloop
call :sleep 2
set /a WAITED+=2
python -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',8050))==0 else 1)" >nul 2>&1
if not errorlevel 1 goto ready
if %WAITED% lss 90 goto waitloop
echo          Dashboard did not answer on port 8050 after %WAITED%s.
echo          Check the "Sentinel - Dashboard" window for the reason.
goto summary

:ready
echo          Dashboard is listening after %WAITED%s.

:: ---------------------------------------------------------------------
:: 5. Browser
:: ---------------------------------------------------------------------
echo [BROWSE] Opening %DASHBOARD_URL%
start "" "%DASHBOARD_URL%"

:summary
echo.
echo =====================================================================
echo  Sentinel is up. Four windows are now open:
echo    - Sentinel - MQTT Broker    verbose broker traffic
echo    - Sentinel - AI Pipeline    ingestion + analysis, heartbeat every 60s
echo    - Sentinel - Dashboard      Dash web server
echo    - Sentinel - Mobile API     REST/WebSocket gateway for the Android app
echo.
echo  Dashboard : %DASHBOARD_URL%
echo  Mobile API: see mobile_api.port in common_files\parameters.json (default 8001)
echo  Broker    : 8883 mTLS (sentinels)  /  1883 localhost (pipeline)
echo  Log file  : %SENTINEL_ROOT%\logs_ast_app\logs_ast_app.log
echo  Mobile API log: %SENTINEL_ROOT%\logs_mobile_api\logs_mobile_api.log
echo.
echo  If the dashboard text looks over-sized, press Ctrl+0 in the browser
echo  to reset that site's zoom to 100%%.
echo =====================================================================
echo.
pause
exit /b 0

:: ---------------------------------------------------------------------
:: Redirect-safe delay. "timeout" aborts when stdin is redirected, which
:: happens when this script is launched from another tool rather than by
:: double-clicking it.
:: ---------------------------------------------------------------------
:sleep
set /a "_TICKS=%~1+1"
ping -n %_TICKS% 127.0.0.1 >nul 2>&1
exit /b 0
