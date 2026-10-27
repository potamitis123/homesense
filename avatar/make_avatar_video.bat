@echo off
:: =====================================================================
::  Avatar video: text -> your face saying it in your voice.
::
::  Runs entirely in the `audioapp` conda env, which already has both
::  Chatterbox and SadTalker's dependencies. Nothing is installed, and
::  dash_env is never touched.
::
::  Renders at 512 with the gfpgan face enhancer by default (slow but the
::  mouth and teeth hold up). Add --fast for a quick draft.
::
::  Language and voice are picked from the text automatically:
::    Greek text   -> multilingual model + my_voice_sample_GR.wav
::    English text -> English model      + my_voice_sample.wav
::
::  Usage:
::    make_avatar_video.bat "Good evening. Everything is quiet."
::    make_avatar_video.bat "Καλησπέρα. Όλα ήσυχα."
::    make_avatar_video.bat --fast "draft to check the wording"
::    make_avatar_video.bat --voice my_voice_sample_GR.wav "Mixed case"
::    make_avatar_video.bat --text-file speech.txt -o briefing.mp4
:: =====================================================================
setlocal

set "AVATAR_DIR=%~dp0"
if "%AVATAR_DIR:~-1%"=="\" set "AVATAR_DIR=%AVATAR_DIR:~0,-1%"
set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"
set "CONDA_ENV=audioapp"

if "%~1"=="" (
    echo Usage: make_avatar_video.bat "text to speak" [options]
    echo        make_avatar_video.bat --text-file speech.txt -o out.mp4
    echo.
    echo Quality is 512 + gfpgan by default. Language and voice sample are
    echo chosen from the text: Greek script uses my_voice_sample_GR.wav.
    echo.
    echo Options: --fast  --still  --size 256  --enhancer none  --lang el
    echo          --voice FILE  --device cpu  --keep-wav
    exit /b 1
)

if not exist "%CONDA_BAT%" (
    echo [FATAL] conda not found at "%CONDA_BAT%".
    exit /b 1
)

call "%CONDA_BAT%" activate %CONDA_ENV%
if errorlevel 1 (
    echo [FATAL] Could not activate the "%CONDA_ENV%" environment.
    exit /b 1
)

:: The Sentinel app can be holding most of the 6 GB of VRAM; the script
:: detects that and drops to CPU rather than fighting it for memory.
python -u "%AVATAR_DIR%\make_avatar_video.py" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Failed with exit code %RC%.
)
exit /b %RC%
