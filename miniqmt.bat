@echo off
chcp 65001 >nul 2>&1
title miniQMT Console

REM ================================================================
REM   miniQMT total control panel (English shell only;
REM   the menu UI is rendered by scripts\_launcher.py menu in UTF-8.
REM   This file stays ASCII-only to avoid CMD codepage issues.)
REM
REM   If you change Python environment, edit PYTHON_EXE below.
REM ================================================================
set "PYTHON_EXE=C:\Users\PC\Anaconda3\envs\python39\python.exe"

set "WORK_DIR=%~dp0"
if "%WORK_DIR:~-1%"=="\" set "WORK_DIR=%WORK_DIR:~0,-1%"
set "LAUNCHER=%WORK_DIR%\scripts\_launcher.py"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python not found: %PYTHON_EXE%
    echo Please open miniqmt.bat in Notepad and fix the PYTHON_EXE path.
    pause
    exit /b 1
)
if not exist "%LAUNCHER%" (
    echo [ERROR] launcher script missing: %LAUNCHER%
    pause
    exit /b 1
)
if not exist "%WORK_DIR%\account_config.json" (
    echo [WARN] account_config.json missing in %WORK_DIR%
    echo You can still use the menu to check environment and install deps,
    echo then create account_config.json before starting any account.
    echo.
    pause
)

"%PYTHON_EXE%" "%LAUNCHER%" menu
exit /b %ERRORLEVEL%
