@echo off
chcp 65001 >nul 2>&1
title miniQMT Console

setlocal EnableExtensions DisableDelayedExpansion

REM ================================================================
REM   miniQMT total control panel (English shell only;
REM   the menu UI is rendered by scripts\_launcher.py menu in UTF-8.
REM   This file stays ASCII-only to avoid CMD codepage issues.)
REM
REM   Python discovery order:
REM     1. MINIQMT_PYTHON_EXE or PYTHON_EXE environment variable
REM     2. Preferred local Anaconda python39 environment
REM     3. launcher.ini ENV_TYPE/UV_VENV_DIR or CONDA_ENV
REM     4. Project .venv / venv
REM     5. python in PATH
REM ================================================================

set "WORK_DIR=%~dp0"
if "%WORK_DIR:~-1%"=="\" set "WORK_DIR=%WORK_DIR:~0,-1%"
set "LAUNCHER=%WORK_DIR%\scripts\_launcher.py"
set "CONFIG_FILE=%WORK_DIR%\launcher.ini"

set "PYTHON_ENV_OVERRIDE=%MINIQMT_PYTHON_EXE%"
if not defined PYTHON_ENV_OVERRIDE if defined PYTHON_EXE set "PYTHON_ENV_OVERRIDE=%PYTHON_EXE%"

set "PYTHON_EXE="
set "PYTHON_SOURCE="
set "PREFERRED_PYTHON_EXE=C:\Users\PC\Anaconda3\envs\python39\python.exe"
set "ENV_TYPE="
set "CONDA_ENV=python39"
set "UV_VENV_DIR=.venv"

if not exist "%LAUNCHER%" (
    echo [ERROR] launcher script missing: %LAUNCHER%
    pause
    exit /b 1
)

if exist "%CONFIG_FILE%" call :ReadConfig "%CONFIG_FILE%"
call :ResolvePython

if not defined PYTHON_EXE (
    echo [ERROR] No usable Python 3.8+ interpreter found.
    echo.
    echo Checked:
    echo   1. MINIQMT_PYTHON_EXE or PYTHON_EXE environment variable
    echo   2. Preferred local Anaconda python39 environment
    echo   3. launcher.ini ENV_TYPE/UV_VENV_DIR or CONDA_ENV
    echo   4. Project .venv / venv
    echo   5. python in PATH
    echo.
    echo Recommended fixes:
    echo   A. Run launcher.bat and select [2] Setup UV Environment
    echo   B. Edit launcher.ini and set ENV_TYPE=uv, UV_VENV_DIR=.venv
    echo   C. Start once with:
    echo      set MINIQMT_PYTHON_EXE=C:\path\to\python.exe
    echo      miniqmt.bat
    echo.
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

:ReadConfig
for /f "usebackq tokens=1* delims==" %%A in ("%~1") do (
    if /i "%%A"=="ENV_TYPE" set "ENV_TYPE=%%B"
    if /i "%%A"=="CONDA_ENV" set "CONDA_ENV=%%B"
    if /i "%%A"=="UV_VENV_DIR" set "UV_VENV_DIR=%%B"
)
exit /b 0

:ResolvePython
if defined PYTHON_ENV_OVERRIDE call :UseCandidate "%PYTHON_ENV_OVERRIDE%" "environment override"
call :UseCandidate "%PREFERRED_PYTHON_EXE%" "preferred Anaconda python39"

if /i "%ENV_TYPE%"=="uv" (
    call :UseCandidate "%WORK_DIR%\%UV_VENV_DIR%\Scripts\python.exe" "launcher.ini UV_VENV_DIR"
)

if /i "%ENV_TYPE%"=="conda" (
    call :ResolveCondaPython "%CONDA_ENV%"
)

call :UseCandidate "%WORK_DIR%\.venv\Scripts\python.exe" "project .venv"
call :UseCandidate "%WORK_DIR%\venv\Scripts\python.exe" "project venv"
call :ResolveCondaPython "%CONDA_ENV%"
call :ResolvePathPython
exit /b 0

:UseCandidate
if defined PYTHON_EXE exit /b 0
set "PYTHON_CANDIDATE=%~1"
if not defined PYTHON_CANDIDATE exit /b 0
if exist "%PYTHON_CANDIDATE%" (
    "%PYTHON_CANDIDATE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=%PYTHON_CANDIDATE%"
        set "PYTHON_SOURCE=%~2"
    )
)
exit /b 0

:ResolveCondaPython
if defined PYTHON_EXE exit /b 0
if "%~1"=="" exit /b 0

if /i "%CONDA_DEFAULT_ENV%"=="%~1" (
    call :UseCandidate "%CONDA_PREFIX%\python.exe" "active conda env"
)

for /f "delims=" %%B in ('conda info --base 2^>nul') do (
    if not defined PYTHON_EXE call :UseCandidate "%%B\envs\%~1\python.exe" "conda info --base"
)

for %%R in (
    "%USERPROFILE%\anaconda3"
    "%USERPROFILE%\miniconda3"
    "C:\ProgramData\Anaconda3"
    "C:\ProgramData\Miniconda3"
    "C:\Anaconda3"
    "C:\Miniconda3"
    "%LOCALAPPDATA%\Continuum\anaconda3"
    "%LOCALAPPDATA%\Continuum\miniconda3"
) do (
    if not defined PYTHON_EXE call :UseCandidate "%%~R\envs\%~1\python.exe" "conda env %~1"
)
exit /b 0

:ResolvePathPython
if defined PYTHON_EXE exit /b 0
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYTHON_EXE call :UseCandidate "%%~fP" "PATH"
)
exit /b 0
