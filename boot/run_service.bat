@echo off
:: =====================================================================
:: Sentinel service supervisor
:: Runs one component inside the conda environment, keeps its console
:: visible, and restarts it if it dies.
::
::   run_service.bat <display-name> <script-relative-path>
:: =====================================================================
setlocal

set "SERVICE_NAME=%~1"
set "SERVICE_SCRIPT=%~2"
title Sentinel - %SERVICE_NAME%

if "%SENTINEL_ROOT%"=="" set "SENTINEL_ROOT=%~dp0.."
if "%CONDA_BAT%"=="" set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"
if "%CONDA_ENV%"=="" set "CONDA_ENV=dash_env"

cd /d "%SENTINEL_ROOT%"

:: torch + numpy both ship OpenMP runtimes; without this CLAP search aborts
:: mid-matmul with "OMP: Error #15" inside the dashboard process.
set "KMP_DUPLICATE_LIB_OK=TRUE"

:: Activating the environment (rather than calling envs\<env>\python.exe by
:: absolute path) is required: it puts %CONDA_PREFIX%\Library\bin on PATH so
:: numpy resolves the matching MKL. Without it np.linalg.inv aborts the
:: interpreter with 0xC06D007F and the service dies during model loading.
call "%CONDA_BAT%" activate %CONDA_ENV%
if errorlevel 1 (
    echo.
    echo [FATAL] Could not activate conda environment "%CONDA_ENV%".
    echo         Looked for: %CONDA_BAT%
    echo.
    pause
    exit /b 1
)

set /a RESTARTS=0

:run
echo.
echo ---------------------------------------------------------------------
echo  %SERVICE_NAME% - starting at %TIME% - restarts: %RESTARTS%
echo  %SERVICE_SCRIPT%
echo ---------------------------------------------------------------------
echo.

python -u "%SERVICE_SCRIPT%"
set "SERVICE_EXIT=%ERRORLEVEL%"

echo.
echo ---------------------------------------------------------------------
if "%SERVICE_EXIT%"=="0" (
    echo  %SERVICE_NAME% exited normally.
    echo ---------------------------------------------------------------------
    pause
    exit /b 0
)
echo  %SERVICE_NAME% STOPPED with exit code %SERVICE_EXIT%
if "%SERVICE_EXIT%"=="-1066598273" echo  0xC06D007F: a native library is missing an expected export.
if "%SERVICE_EXIT%"=="3221225477" echo  0xC0000005: access violation inside a native library.
echo  Restarting in 10 seconds - close this window to stop.
echo ---------------------------------------------------------------------
ping -n 11 127.0.0.1 >nul 2>&1
set /a RESTARTS+=1
goto run
