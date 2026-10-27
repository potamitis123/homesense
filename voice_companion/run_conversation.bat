@echo off
:: Runs in the audioapp env, which owns langgraph, piper, whisper and
:: openai. dash_env is never touched by this project.
setlocal
set "VC_DIR=%~dp0"
if "%VC_DIR:~-1%"=="\" set "VC_DIR=%VC_DIR:~0,-1%"
set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"
if not exist "%CONDA_BAT%" (
    echo [FATAL] conda not found at "%CONDA_BAT%".
    exit /b 1
)
call "%CONDA_BAT%" activate audioapp
if errorlevel 1 (
    echo [FATAL] Could not activate the audioapp environment.
    exit /b 1
)
python -u "%VC_DIR%\run_conversation.py" %*
exit /b %ERRORLEVEL%
