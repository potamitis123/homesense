@echo off
setlocal
set "VC_DIR=%~dp0"
if "%VC_DIR:~-1%"=="\" set "VC_DIR=%VC_DIR:~0,-1%"
call "%USERPROFILE%\anaconda3\condabin\conda.bat" activate audioapp
if errorlevel 1 exit /b 1
python -m pytest "%VC_DIR%\tests" -q %*
exit /b %ERRORLEVEL%
