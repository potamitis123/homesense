@echo off
:: =====================================================================
::  Prepare a raw recording into a Chatterbox voice reference.
::
::    prep_voice.bat                          my_voice_sample.wav -> *_prepped.wav
::    prep_voice.bat new_recording.wav -o my_voice_en.wav
::    prep_voice.bat --seconds 8 new_recording.wav
:: =====================================================================
setlocal

set "AVATAR_DIR=%~dp0"
if "%AVATAR_DIR:~-1%"=="\" set "AVATAR_DIR=%AVATAR_DIR:~0,-1%"
set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"

if not exist "%CONDA_BAT%" (
    echo [FATAL] conda not found at "%CONDA_BAT%".
    exit /b 1
)

call "%CONDA_BAT%" activate audioapp
if errorlevel 1 (
    echo [FATAL] Could not activate the "audioapp" environment.
    exit /b 1
)

python -u "%AVATAR_DIR%\prep_voice.py" %*
exit /b %ERRORLEVEL%
