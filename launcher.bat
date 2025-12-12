
@echo off
setlocal enabledelayedexpansion

:: ====================================
:: Python Project Launcher
:: ====================================

:: Set config file path
set "CONFIG_FILE=%~dp0launcher.ini"

:: Read configuration
call :ReadConfig

:MainMenu
cls
echo ====================================================
echo        Python Project Launcher                     
echo ====================================================
echo.
echo Current Configuration:
echo   [1] Conda Env:     %CONDA_ENV%
echo   [2] Work Dir:      %WORK_DIR%
echo   [3] Python Script: %PYTHON_SCRIPT%
echo   [4] Script Args:   %SCRIPT_ARGS%
echo.
echo ----------------------------------------------------
echo Menu Options:
echo   [1] Run Python Script
echo   [2] Update Repository (git pull)
echo   [3] Modify CONDA_ENV
echo   [4] Modify WORK_DIR
echo   [5] Modify PYTHON_SCRIPT
echo   [6] Modify SCRIPT_ARGS
echo   [0] Exit
echo ----------------------------------------------------
echo.
set /p choice="Select option [0-6]: "

if "%choice%"=="1" goto RunScript
if "%choice%"=="2" goto UpdateRepo
if "%choice%"=="3" goto ModifyCONDA
if "%choice%"=="4" goto ModifyWORKDIR
if "%choice%"=="5" goto ModifyScript
if "%choice%"=="6" goto ModifyArgs
if "%choice%"=="0" goto End

echo.
echo [ERROR] Invalid choice, please try again
timeout /t 2 >nul
goto MainMenu

:: ====================================
:: Function 1: Run Python Script
:: ====================================
:RunScript
cls
echo ====================================================
echo  Run Python Script
echo ====================================================
echo.
echo Conda Env:     %CONDA_ENV%
echo Work Dir:      %WORK_DIR%
echo Script Path:   %PYTHON_SCRIPT%
echo Script Args:   %SCRIPT_ARGS%
echo.
echo Starting...
echo ----------------------------------------------------
echo.

:: Change to work directory
if not exist "%WORK_DIR%" (
    echo [ERROR] Work directory does not exist: %WORK_DIR%
    pause
    goto MainMenu
)
cd /d "%WORK_DIR%"

:: Check if script exists
if not exist "%PYTHON_SCRIPT%" (
    echo [ERROR] Python script does not exist: %PYTHON_SCRIPT%
    pause
    goto MainMenu
)

:: Initialize Conda (try multiple common paths)
set "CONDA_INITIALIZED=0"

:: Method 1: Try conda hook if already in PATH
where conda >nul 2>&1
if %errorlevel%==0 (
    set "CONDA_INITIALIZED=1"
    goto :CondaReady
)

:: Method 2: Try common Anaconda installation paths
set "CONDA_PATHS[0]=%USERPROFILE%\anaconda3"
set "CONDA_PATHS[1]=%USERPROFILE%\miniconda3"
set "CONDA_PATHS[2]=C:\ProgramData\Anaconda3"
set "CONDA_PATHS[3]=C:\ProgramData\Miniconda3"
set "CONDA_PATHS[4]=C:\Anaconda3"
set "CONDA_PATHS[5]=C:\Miniconda3"
set "CONDA_PATHS[6]=%LOCALAPPDATA%\Continuum\anaconda3"
set "CONDA_PATHS[7]=%LOCALAPPDATA%\Continuum\miniconda3"

for /L %%i in (0,1,7) do (
    if exist "!CONDA_PATHS[%%i]!\Scripts\conda.exe" (
        echo [INFO] Found Conda at: !CONDA_PATHS[%%i]!
        call "!CONDA_PATHS[%%i]!\Scripts\activate.bat" "!CONDA_PATHS[%%i]!"
        set "CONDA_INITIALIZED=1"
        goto :CondaReady
    )
)

:: Method 3: Try condabin path
for /L %%i in (0,1,7) do (
    if exist "!CONDA_PATHS[%%i]!\condabin\conda.bat" (
        echo [INFO] Found Conda at: !CONDA_PATHS[%%i]!
        call "!CONDA_PATHS[%%i]!\condabin\conda.bat" activate base
        set "CONDA_INITIALIZED=1"
        goto :CondaReady
    )
)

:CondaReady
if "%CONDA_INITIALIZED%"=="0" (
    echo.
    echo [ERROR] Cannot find Conda installation
    echo.
    echo Please ensure Conda is installed and try one of these solutions:
    echo   1. Add Conda to PATH environment variable
    echo   2. Run this script from Anaconda Prompt
    echo   3. Install Anaconda/Miniconda
    echo.
    pause
    goto MainMenu
)

:: Activate Conda environment
echo [INFO] Activating environment: %CONDA_ENV%
call conda activate %CONDA_ENV%
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to activate Conda environment: %CONDA_ENV%
    echo.
    echo Available environments:
    call conda env list
    echo.
    pause
    goto MainMenu
)

:: Run Python script
echo.
echo [EXECUTING] python %PYTHON_SCRIPT% %SCRIPT_ARGS%
echo.
python %PYTHON_SCRIPT% %SCRIPT_ARGS%

echo.
echo ----------------------------------------------------
echo Script execution completed (exit code: %errorlevel%)
echo.
pause
goto MainMenu

:: ====================================
:: Function 2: Update Repository
:: ====================================
:UpdateRepo
cls
echo ====================================================
echo  Update Repository (Git Pull)
echo ====================================================
echo.
echo Work Dir: %WORK_DIR%
echo.

:: Change to work directory
if not exist "%WORK_DIR%" (
    echo [ERROR] Work directory does not exist: %WORK_DIR%
    pause
    goto MainMenu
)
cd /d "%WORK_DIR%"

:: Check if it's a Git repository
if not exist ".git" (
    echo [ERROR] Current directory is not a Git repository
    echo Directory: %WORK_DIR%
    pause
    goto MainMenu
)

:: Check if git is available
where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git is not installed or not in PATH
    echo Please install Git and try again
    pause
    goto MainMenu
)

:: Show current branch and status
echo [INFO] Current Git status:
echo ----------------------------------------------------
git status -sb
echo.

:: Execute git pull
echo [EXECUTING] git pull
echo ----------------------------------------------------
git pull

if errorlevel 1 (
    echo.
    echo [ERROR] Git pull failed
    pause
    goto MainMenu
)

echo.
echo ----------------------------------------------------
echo [SUCCESS] Repository updated successfully
echo.
pause
goto MainMenu

:: ====================================
:: Function 3: Modify CONDA_ENV
:: ====================================
:ModifyCONDA
cls
echo ====================================================
echo  Modify Conda Environment Name
echo ====================================================
echo.
echo Current value: %CONDA_ENV%
echo.
set /p new_conda="Enter new environment name (press Enter to keep current): "

if "%new_conda%"=="" (
    echo [INFO] No changes made
    timeout /t 1 >nul
    goto MainMenu
)

set "CONDA_ENV=%new_conda%"
call :WriteConfig
echo.
echo [SUCCESS] CONDA_ENV updated to: %CONDA_ENV%
timeout /t 2 >nul
goto MainMenu

:: ====================================
:: Function 4: Modify WORK_DIR
:: ====================================
:ModifyWORKDIR
cls
echo ====================================================
echo  Modify Work Directory
echo ====================================================
echo.
echo Current value: %WORK_DIR%
echo.
set /p new_workdir="Enter new work directory (press Enter to keep current): "

if "%new_workdir%"=="" (
    echo [INFO] No changes made
    timeout /t 1 >nul
    goto MainMenu
)

:: Validate directory exists
if not exist "%new_workdir%" (
    echo.
    echo [WARNING] Directory does not exist: %new_workdir%
    set /p confirm="Do you still want to save? (Y/N): "
    if /i not "!confirm!"=="Y" goto MainMenu
)

set "WORK_DIR=%new_workdir%"
call :WriteConfig
echo.
echo [SUCCESS] WORK_DIR updated to: %WORK_DIR%
timeout /t 2 >nul
goto MainMenu

:: ====================================
:: Function 5: Modify PYTHON_SCRIPT
:: ====================================
:ModifyScript
cls
echo ====================================================
echo  Modify Python Script Path
echo ====================================================
echo.
echo Current value: %PYTHON_SCRIPT%
echo.
set /p new_script="Enter new script path (press Enter to keep current): "

if "%new_script%"=="" (
    echo [INFO] No changes made
    timeout /t 1 >nul
    goto MainMenu
)

set "PYTHON_SCRIPT=%new_script%"
call :WriteConfig
echo.
echo [SUCCESS] PYTHON_SCRIPT updated to: %PYTHON_SCRIPT%
timeout /t 2 >nul
goto MainMenu

:: ====================================
:: Function 6: Modify SCRIPT_ARGS
:: ====================================
:ModifyArgs
cls
echo ====================================================
echo  Modify Script Arguments
echo ====================================================
echo.
echo Current value: %SCRIPT_ARGS%
echo.
echo Example: --config prod --port 8080
echo          Leave empty for no arguments
echo.
set /p new_args="Enter new arguments (press Enter to clear): "

set "SCRIPT_ARGS=%new_args%"
call :WriteConfig
echo.
echo [SUCCESS] SCRIPT_ARGS updated to: %SCRIPT_ARGS%
timeout /t 2 >nul
goto MainMenu

:: ====================================
:: Read Configuration File
:: ====================================
:ReadConfig
:: Set default values
set "CONDA_ENV=python39"
set "WORK_DIR=%~dp0"
set "PYTHON_SCRIPT=main.py"
set "SCRIPT_ARGS="

:: If config file doesn't exist, create default config
if not exist "%CONFIG_FILE%" (
    call :WriteConfig
    goto :eof
)

:: Read configuration
for /f "usebackq tokens=1,* delims==" %%a in ("%CONFIG_FILE%") do (
    set "line=%%a"
    set "value=%%b"
    
    :: Skip comments and empty lines
    if not "!line:~0,1!"=="#" if not "!line:~0,1!"=="[" if not "!line!"=="" (
        if "!line!"=="CONDA_ENV" set "CONDA_ENV=!value!"
        if "!line!"=="WORK_DIR" set "WORK_DIR=!value!"
        if "!line!"=="PYTHON_SCRIPT" set "PYTHON_SCRIPT=!value!"
        if "!line!"=="SCRIPT_ARGS" set "SCRIPT_ARGS=!value!"
    )
)

:: Remove leading/trailing spaces
for /f "tokens=* delims= " %%a in ("%CONDA_ENV%") do set "CONDA_ENV=%%a"
for /f "tokens=* delims= " %%a in ("%WORK_DIR%") do set "WORK_DIR=%%a"
for /f "tokens=* delims= " %%a in ("%PYTHON_SCRIPT%") do set "PYTHON_SCRIPT=%%a"

goto :eof

:: ====================================
:: Write Configuration File
:: ====================================
:WriteConfig
(
    echo [Environment]
    echo # Conda virtual environment name
    echo CONDA_ENV=%CONDA_ENV%
    echo.
    echo [Project]
    echo # Project working directory
    echo WORK_DIR=%WORK_DIR%
    echo.
    echo [Script]
    echo # Python script to run
    echo PYTHON_SCRIPT=%PYTHON_SCRIPT%
    echo.
    echo # Script arguments ^(optional^)
    echo # Example: SCRIPT_ARGS=--config prod --port 8080
    echo SCRIPT_ARGS=%SCRIPT_ARGS%
) > "%CONFIG_FILE%"
goto :eof

:: ====================================
:: Exit
:: ====================================
:End
echo.
echo Thank you for using Python Project Launcher!
timeout /t 1 >nul
exit /b 0
