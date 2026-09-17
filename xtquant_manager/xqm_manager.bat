@echo off
setlocal enabledelayedexpansion

:: ============================================================
::  XtQuantManager Service Manager
::  Usage: xqm_manager.bat [start|stop|restart|status|ui|logs]
::  No arguments: interactive menu
:: ============================================================

set "WORK_DIR=%~dp0"
set "PROJECT_DIR=%~dp0..\"
set "PYTHON=C:\Users\PC\Anaconda3\envs\python39\python.exe"
set "MODULE=-m xtquant_manager"
set "HOST=127.0.0.1"
set "PORT=8888"
set "PID_FILE=%WORK_DIR%.xqm_manager.pid"
set "LOG_FILE=%PROJECT_DIR%logs\xqm_manager.log"
set "LOG_MAX_BYTES=10485760"
set "LOG_BACKUP_COUNT=5"
set "UI_A=%WORK_DIR%test_ui\test_ui_a.html"
set "UI_B=%WORK_DIR%test_ui\test_ui_b.html"
set "HEALTH_URL=http://%HOST%:%PORT%/api/v1/health"

:: Command-line argument mode
if /i "%~1"=="start"   goto CmdStart
if /i "%~1"=="stop"    goto CmdStop
if /i "%~1"=="restart" goto CmdRestart
if /i "%~1"=="status"  goto CmdStatus
if /i "%~1"=="ui"      goto CmdOpenUI
if /i "%~1"=="logs"    goto CmdLogs
if not "%~1"=="" (
    echo [ERROR] Unknown command: %~1
    echo Usage: %~nx0 [start^|stop^|restart^|status^|ui^|logs]
    exit /b 1
)

:: ============================================================
::  Interactive Menu
:: ============================================================
:MainMenu
cls
echo ============================================================
echo   XtQuantManager Service Manager
echo ============================================================
echo.
call :ShowStatusLine
echo.
echo   [1] Start service
echo   [2] Stop service
echo   [3] Restart service
echo   [4] Show full status
echo   [5] Open Test UI-A (functional)
echo   [6] Open Test UI-B (visual)
echo   [7] Tail logs (live)
echo   [0] Exit
echo.
echo ------------------------------------------------------------
set /p choice="Select [0-7]: "

if "%choice%"=="1" ( cls & call :StartService & pause & goto MainMenu )
if "%choice%"=="2" ( cls & call :StopService  & pause & goto MainMenu )
if "%choice%"=="3" ( cls & call :StopService & timeout /t 2 >nul & call :StartService & pause & goto MainMenu )
if "%choice%"=="4" ( cls & call :ShowFullStatus & pause & goto MainMenu )
if "%choice%"=="5" ( call :OpenUI "%UI_A%" & goto MainMenu )
if "%choice%"=="6" ( call :OpenUI "%UI_B%" & goto MainMenu )
if "%choice%"=="7" ( cls & call :TailLogs & goto MainMenu )
if "%choice%"=="0" goto End

echo [ERROR] Invalid choice
timeout /t 1 >nul
goto MainMenu

:: ============================================================
::  Command-line entry points
:: ============================================================
:CmdStart
call :StartService
exit /b %errorlevel%

:CmdStop
call :StopService
exit /b %errorlevel%

:CmdRestart
call :StopService
timeout /t 2 >nul
call :StartService
exit /b %errorlevel%

:CmdStatus
call :ShowFullStatus
exit /b 0

:CmdOpenUI
call :OpenUI "%UI_A%"
exit /b 0

:CmdLogs
call :TailLogs
exit /b 0

:: ============================================================
::  Core subroutines
:: ============================================================

:: ---- Start service ------------------------------------------
:StartService
echo [start] Checking port %PORT%...
call :IsPortInUse
if !PORT_USED!==1 (
    call :GetHealth
    if !HEALTH_OK!==1 (
        echo [info] Service already running and healthy - skipping start
        goto :eof
    )
    echo [warn] Port %PORT% occupied but health check failed - cleaning up...
    call :KillByPort
)

if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"
call :RotateLog

echo [start] Launching XtQuantManager on %HOST%:%PORT%...
cd /d "%PROJECT_DIR%"
start "XtQuantManager:%PORT%" /min cmd /c ^
    "%PYTHON%" %MODULE% --host %HOST% --port %PORT% ^
    >> "%LOG_FILE%" 2>&1

echo [start] Waiting for service to become ready (max 15s)...
set /a WAIT_SEC=0
:WaitReady
timeout /t 1 >nul
set /a WAIT_SEC+=1
call :GetHealth
if !HEALTH_OK!==1 goto :StartOK
if !WAIT_SEC! geq 15 (
    echo [error] Service did not respond within 15 seconds
    echo [info]  Log: %LOG_FILE%
    goto :eof
)
goto WaitReady

:StartOK
call :SavePID
echo [OK] Service started  ^|  http://%HOST%:%PORT%  ^|  PID: !SAVED_PID!
goto :eof


:: ---- Stop service -------------------------------------------
:StopService
echo [stop] Stopping XtQuantManager...

if exist "%PID_FILE%" (
    for /f "usebackq tokens=*" %%a in ("%PID_FILE%") do set SAVED_PID=%%a
    if not "!SAVED_PID!"=="" (
        echo [info] Killing PID: !SAVED_PID!
        taskkill /PID !SAVED_PID! /F >nul 2>&1
        del "%PID_FILE%" >nul 2>&1
        call :IsPortInUse
        if !PORT_USED!==0 ( echo [OK] Service stopped & goto :eof )
    )
)

call :KillByPort
call :IsPortInUse
if !PORT_USED!==0 (
    echo [OK] Service stopped
) else (
    echo [warn] Service was not running on port %PORT%
)
goto :eof


:: ---- Full status display ------------------------------------
:ShowFullStatus
echo ============================================================
echo   XtQuantManager Status
echo ============================================================
echo.

call :IsPortInUse
if !PORT_USED!==1 (
    echo   Process : Running  (port %PORT% is listening)
) else (
    echo   Process : Stopped
)

if exist "%PID_FILE%" (
    set /p SAVED_PID=< "%PID_FILE%"
    echo   PID     : !SAVED_PID!
) else (
    echo   PID     : (not recorded)
)

echo.
echo   Health check: %HEALTH_URL%
curl -s -w "  HTTP %%{http_code} ^| %%{time_total}s\n" %HEALTH_URL% 2>nul
if errorlevel 1 echo   [error] Cannot connect to service

echo.
echo   Log: %LOG_FILE%
if exist "%LOG_FILE%" (
    for %%f in ("%LOG_FILE%") do echo   Size: %%~zf bytes
    echo.
    echo   --- Last 10 lines ---
    powershell -command "Get-Content '%LOG_FILE%' -Tail 10 -ErrorAction SilentlyContinue" 2>nul
) else (
    echo   (log file does not exist yet)
)
goto :eof


:: ---- Open test UI -------------------------------------------
:OpenUI
set "UI_PATH=%~1"
if not exist "!UI_PATH!" (
    echo [error] File not found: !UI_PATH!
    goto :eof
)
echo [info] Opening test UI...
start "" "!UI_PATH!"
goto :eof


:: ---- Live log tail ------------------------------------------
:TailLogs
if not exist "%LOG_FILE%" (
    echo [info] Log file not found: %LOG_FILE%
    echo [info] Start the service first
    pause
    goto :eof
)
echo [info] Tailing log (Ctrl+C to stop)
echo ============================================================
powershell -command "Get-Content '%LOG_FILE%' -Wait -Tail 30"
goto :eof


:: ---- Rotate append-only log before startup ------------------
:RotateLog
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p='%LOG_FILE%'; $max=%LOG_MAX_BYTES%; $n=%LOG_BACKUP_COUNT%; if (Test-Path $p) { $f=Get-Item $p; if ($f.Length -ge $max) { $old=\"$p.$n\"; if (Test-Path $old) { Remove-Item $old -Force }; for ($i=$n-1; $i -ge 1; $i--) { $src=\"$p.$i\"; $dst=\"$p.\" + ($i+1); if (Test-Path $src) { Move-Item $src $dst -Force } }; Move-Item $p \"$p.1\" -Force; New-Item -ItemType File -Path $p -Force | Out-Null } }" >nul 2>&1
goto :eof


:: ============================================================
::  Utility subroutines
:: ============================================================

:: ---- Check if port is in use (sets PORT_USED=0/1) ----------
:IsPortInUse
set PORT_USED=0
for /f "tokens=*" %%a in ('netstat -ano 2^>nul ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    set PORT_USED=1
)
goto :eof

:: ---- Health check (sets HEALTH_OK=0/1) ---------------------
:GetHealth
set HEALTH_OK=0
for /f %%a in ('curl -s -o nul -w "%%{http_code}" --max-time 3 %HEALTH_URL% 2^>nul') do (
    if "%%a"=="200" set HEALTH_OK=1
)
goto :eof

:: ---- Kill process listening on PORT -------------------------
:KillByPort
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" (
        echo [info] Killing PID: %%a
        taskkill /PID %%a /F >nul 2>&1
    )
)
goto :eof

:: ---- Save PID to file (reads from netstat) -----------------
:SavePID
set SAVED_PID=
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    set SAVED_PID=%%a
)
if not "!SAVED_PID!"=="" >"%PID_FILE%" echo !SAVED_PID!
goto :eof

:: ---- One-line status for menu header -----------------------
:ShowStatusLine
call :IsPortInUse
call :GetHealth
if !HEALTH_OK!==1 (
    echo   Status: [RUNNING]  http://%HOST%:%PORT%  (health OK)
) else if !PORT_USED!==1 (
    echo   Status: [UNHEALTHY] port %PORT% occupied but health check failed
) else (
    echo   Status: [STOPPED]
)
goto :eof

:: ============================================================
:End
echo.
exit /b 0
