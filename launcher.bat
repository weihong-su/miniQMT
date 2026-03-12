
@echo off
setlocal enabledelayedexpansion

:: ====================================
:: Python Project Launcher
:: ====================================

:: Set config file path
set "CONFIG_FILE=%~dp0launcher.ini"

:: Read configuration
call :ReadConfig

:: ====================================
:: Auto-Start (5-second countdown)
:: ====================================
:AutoStart
cls
echo ====================================================
echo  Python Project Launcher
echo ====================================================
echo.
echo  Script:   %PYTHON_SCRIPT%
echo  Work Dir: %WORK_DIR%
echo  Env:      %ENV_TYPE%  /  %UV_VENV_DIR%
echo.

:: Check project is ready before auto-run
set "READY=1"
if not exist "%WORK_DIR%" set "READY=0"
if "%READY%"=="1" (
    if not exist "%WORK_DIR%\%PYTHON_SCRIPT%" set "READY=0"
)
if "%READY%"=="1" (
    if /i "%ENV_TYPE%"=="uv" (
        if not exist "%WORK_DIR%\%UV_VENV_DIR%\Scripts\python.exe" set "READY=0"
    )
)

if "%READY%"=="0" (
    echo  [INFO] Project not ready. Opening menu...
    timeout /t 2 >nul
    goto MainMenu
)

echo ----------------------------------------------------
echo  Auto-starting in 5 seconds...
echo  Press M to open the menu
echo ----------------------------------------------------
echo.
choice /c MR /n /t 5 /d R
if errorlevel 2 goto RunScript
goto MainMenu

:MainMenu
cls
echo ====================================================
echo        Python Project Launcher
echo ====================================================
echo.
echo Current Configuration:
echo   Conda Env:    %CONDA_ENV%
echo   Work Dir:     %WORK_DIR%
echo   Script:       %PYTHON_SCRIPT%
echo   Args:         %SCRIPT_ARGS%
echo   GitHub Repo:  %GITHUB_REPO%
echo   Env Type:     %ENV_TYPE%
echo   UV Venv Dir:  %UV_VENV_DIR%
echo.
echo ----------------------------------------------------
echo  [Setup]
echo   [1] Clone Repository from GitHub
echo   [2] Setup UV Environment (install uv + dependencies)
echo.
echo  [Run]
echo   [3] Run Python Script
echo   [4] Update Repository (git pull)
echo.
echo  [Config]
echo   [5] Modify CONDA_ENV
echo   [6] Modify WORK_DIR
echo   [7] Modify PYTHON_SCRIPT
echo   [8] Modify SCRIPT_ARGS
echo   [9] Modify GITHUB_REPO
echo   [A] Modify ENV_TYPE  (conda / uv)
echo   [B] Modify UV_VENV_DIR
echo.
echo   [0] Exit
echo ----------------------------------------------------
echo.
set /p choice="Select option [0-9 / A-B]: "

if /i "%choice%"=="1" goto CloneRepo
if /i "%choice%"=="2" goto SetupUV
if /i "%choice%"=="3" goto RunScript
if /i "%choice%"=="4" goto UpdateRepo
if /i "%choice%"=="5" goto ModifyCONDA
if /i "%choice%"=="6" goto ModifyWORKDIR
if /i "%choice%"=="7" goto ModifyScript
if /i "%choice%"=="8" goto ModifyArgs
if /i "%choice%"=="9" goto ModifyGitHubRepo
if /i "%choice%"=="A" goto ModifyEnvType
if /i "%choice%"=="B" goto ModifyUVVenvDir
if /i "%choice%"=="0" goto End

echo.
echo [ERROR] Invalid choice, please try again
timeout /t 2 >nul
goto MainMenu

:: ====================================
:: Function 1: Clone Repository from GitHub
:: ====================================
:CloneRepo
cls
echo ====================================================
echo  Clone Repository from GitHub
echo ====================================================
echo.

:: Check if git is installed
where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git is not installed or not in PATH
    echo.
    echo Please install Git for Windows:
    echo   https://git-scm.com/download/windows
    echo.
    echo After installation, restart this script.
    echo.
    pause
    goto MainMenu
)

:: Check if GITHUB_REPO is configured
if "%GITHUB_REPO%"=="" (
    echo [WARNING] GitHub repository URL not configured
    echo.
    set /p GITHUB_REPO="Enter GitHub repository URL: "
    if "!GITHUB_REPO!"=="" (
        echo [ERROR] Repository URL cannot be empty
        pause
        goto MainMenu
    )
    call :WriteConfig
)

echo GitHub Repo: %GITHUB_REPO%
echo Target Dir:  %WORK_DIR%
echo.

:: Check if target directory already has a git repo
if exist "%WORK_DIR%\.git" (
    echo [WARNING] Directory already contains a Git repository: %WORK_DIR%
    set /p confirm="Re-clone? This will DELETE the existing directory! (Y/N): "
    if /i not "!confirm!"=="Y" (
        echo [INFO] Clone cancelled
        pause
        goto MainMenu
    )
    echo [INFO] Removing existing directory...
    rmdir /s /q "%WORK_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to remove existing directory
        pause
        goto MainMenu
    )
)

:: Create parent directory if it doesn't exist
for %%i in ("%WORK_DIR%") do set "PARENT_DIR=%%~dpi"
if not exist "!PARENT_DIR!" (
    echo [INFO] Creating parent directory: !PARENT_DIR!
    mkdir "!PARENT_DIR!"
)

:: Clone repository
echo [EXECUTING] git clone "%GITHUB_REPO%" "%WORK_DIR%"
echo ----------------------------------------------------
git clone "%GITHUB_REPO%" "%WORK_DIR%"

if errorlevel 1 (
    echo.
    echo [ERROR] Git clone failed
    echo Please check:
    echo   1. Repository URL is correct
    echo   2. You have network access to GitHub
    echo   3. You have permission to access the repository
    pause
    goto MainMenu
)

echo.
echo ----------------------------------------------------
echo [SUCCESS] Repository cloned to: %WORK_DIR%
echo.
pause
goto MainMenu

:: ====================================
:: Function 2: Setup UV Environment
:: ====================================
:SetupUV
cls
echo ====================================================
echo  Setup UV Environment
echo ====================================================
echo.
echo Work Dir:     %WORK_DIR%
echo Venv Dir:     %UV_VENV_DIR%
echo Requirements: %REQUIREMENTS_FILE%
echo.

:: Check if work directory exists
if not exist "%WORK_DIR%" (
    echo [ERROR] Work directory does not exist: %WORK_DIR%
    echo Please clone the repository first (option [1])
    echo.
    pause
    goto MainMenu
)

cd /d "%WORK_DIR%"

:: ---- Step 1: Install uv ----
echo [STEP 1/3] Checking uv installation...
where uv >nul 2>&1
if errorlevel 1 (
    echo [INFO] uv not found, installing via PowerShell...
    echo [EXECUTING] powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install uv via PowerShell
        echo.
        echo Please install uv manually using ONE of the following methods:
        echo.
        echo   Method 1 - PowerShell:
        echo     powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
        echo.
        echo   Method 2 - pip:
        echo     pip install uv
        echo.
        echo After installation, restart this script.
        pause
        goto MainMenu
    )
    :: Refresh PATH so uv is available in the current session
    for /f "tokens=*" %%p in ('powershell -c "[System.Environment]::GetEnvironmentVariable(\"PATH\",\"User\")"') do (
        set "PATH=%%p;%PATH%"
    )
    echo [SUCCESS] uv installed successfully
) else (
    for /f "tokens=*" %%v in ('uv --version 2^>^&1') do echo [INFO] Already installed: %%v
)

:: ---- Step 2: Create virtual environment ----
echo.
echo [STEP 2/3] Creating virtual environment at: %UV_VENV_DIR%
if exist "%UV_VENV_DIR%\Scripts\python.exe" (
    echo [INFO] Virtual environment already exists
    set /p recreate="Recreate it? (Y/N, default N): "
    if /i "!recreate!"=="Y" (
        echo [INFO] Removing existing environment...
        rmdir /s /q "%UV_VENV_DIR%"
    ) else (
        echo [INFO] Keeping existing environment
        goto :InstallDeps
    )
)

uv venv "%UV_VENV_DIR%"
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to create virtual environment
    echo Try specifying a Python version: uv venv --python 3.9
    pause
    goto MainMenu
)
echo [SUCCESS] Virtual environment created at: %UV_VENV_DIR%

:: ---- Step 3: Install dependencies ----
:InstallDeps
echo.
echo [STEP 3/3] Installing dependencies from: %REQUIREMENTS_FILE%

if not exist "%REQUIREMENTS_FILE%" (
    echo [WARNING] Requirements file not found: %REQUIREMENTS_FILE%
    echo Searching for requirements files...
    dir /b /s requirements*.txt 2>nul
    echo.
    set /p req_path="Enter requirements file path (relative to work dir): "
    if "!req_path!"=="" (
        echo [INFO] Skipping dependency installation
        goto :SetupUVDone
    )
    set "REQUIREMENTS_FILE=!req_path!"
    call :WriteConfig
)

set "UV_PYTHON=%WORK_DIR%\%UV_VENV_DIR%\Scripts\python.exe"
echo [EXECUTING] uv pip install -r "%REQUIREMENTS_FILE%" --python "%UV_PYTHON%"
echo ----------------------------------------------------
uv pip install -r "%REQUIREMENTS_FILE%" --python "%UV_PYTHON%"

if errorlevel 1 (
    echo.
    echo [WARNING] Some packages may have failed to install
    echo Note: 'xtquant' requires manual installation from QMT client software
    echo       Other packages should install normally
) else (
    echo [SUCCESS] All dependencies installed
)

:SetupUVDone
echo.
echo ----------------------------------------------------
echo [SUCCESS] UV environment setup complete!
echo.
pause
goto MainMenu

:: ====================================
:: Function 3: Run Python Script
:: ====================================
:RunScript
cls
echo ====================================================
echo  Run Python Script
echo ====================================================
echo.
echo Env Type:      %ENV_TYPE%
echo Work Dir:      %WORK_DIR%
echo Script Path:   %PYTHON_SCRIPT%
echo Script Args:   %SCRIPT_ARGS%
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

:: ---- UV Environment Mode ----
if /i "%ENV_TYPE%"=="uv" (
    echo [INFO] Using UV virtual environment: %UV_VENV_DIR%
    echo.

    if not exist "%UV_VENV_DIR%\Scripts\python.exe" (
        echo [ERROR] UV virtual environment not found: %UV_VENV_DIR%\Scripts\python.exe
        echo Please run option [2] to setup the UV environment first
        pause
        goto MainMenu
    )

    echo [EXECUTING] %UV_VENV_DIR%\Scripts\python.exe %PYTHON_SCRIPT% %SCRIPT_ARGS%
    echo ----------------------------------------------------
    echo.
    "%UV_VENV_DIR%\Scripts\python.exe" %PYTHON_SCRIPT% %SCRIPT_ARGS%
    goto :RunScriptDone
)

:: ---- Conda Environment Mode (default) ----
echo [INFO] Using Conda environment: %CONDA_ENV%
echo.
echo Starting...
echo ----------------------------------------------------
echo.

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
    echo Solutions:
    echo   1. Add Conda to PATH environment variable
    echo   2. Run this script from Anaconda Prompt
    echo   3. Install Anaconda/Miniconda
    echo   4. Or use option [A] to switch ENV_TYPE to 'uv' and setup UV environment
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

:RunScriptDone
echo.
echo ----------------------------------------------------
echo Script execution completed (exit code: %errorlevel%)
echo.
pause
goto MainMenu

:: ====================================
:: Function 4: Update Repository
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
    echo.
    echo Use option [1] to clone the repository first
    pause
    goto MainMenu
)

:: Check if git is available
where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git is not installed or not in PATH
    echo.
    echo Please install Git for Windows:
    echo   https://git-scm.com/download/windows
    echo.
    echo After installation, restart this script.
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
:: Function 5: Modify CONDA_ENV
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
:: Function 6: Modify WORK_DIR
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
:: Function 7: Modify PYTHON_SCRIPT
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
:: Function 8: Modify SCRIPT_ARGS
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
:: Function 9: Modify GITHUB_REPO
:: ====================================
:ModifyGitHubRepo
cls
echo ====================================================
echo  Modify GitHub Repository URL
echo ====================================================
echo.
echo Current value: %GITHUB_REPO%
echo.
echo Example: https://github.com/username/repo.git
echo.
set /p new_repo="Enter new GitHub repository URL (press Enter to keep current): "

if "%new_repo%"=="" (
    echo [INFO] No changes made
    timeout /t 1 >nul
    goto MainMenu
)

set "GITHUB_REPO=%new_repo%"
call :WriteConfig
echo.
echo [SUCCESS] GITHUB_REPO updated to: %GITHUB_REPO%
timeout /t 2 >nul
goto MainMenu

:: ====================================
:: Function A: Modify ENV_TYPE
:: ====================================
:ModifyEnvType
cls
echo ====================================================
echo  Modify Environment Type
echo ====================================================
echo.
echo Current value: %ENV_TYPE%
echo.
echo Options:
echo   conda  - Use Conda virtual environment (default)
echo   uv     - Use UV virtual environment (.venv)
echo.
set /p new_env_type="Enter new ENV_TYPE (conda / uv, press Enter to keep current): "

if "%new_env_type%"=="" (
    echo [INFO] No changes made
    timeout /t 1 >nul
    goto MainMenu
)

if /i not "%new_env_type%"=="conda" if /i not "%new_env_type%"=="uv" (
    echo [ERROR] Invalid value: must be 'conda' or 'uv'
    pause
    goto MainMenu
)

set "ENV_TYPE=%new_env_type%"
call :WriteConfig
echo.
echo [SUCCESS] ENV_TYPE updated to: %ENV_TYPE%
timeout /t 2 >nul
goto MainMenu

:: ====================================
:: Function B: Modify UV_VENV_DIR
:: ====================================
:ModifyUVVenvDir
cls
echo ====================================================
echo  Modify UV Virtual Environment Directory
echo ====================================================
echo.
echo Current value: %UV_VENV_DIR%
echo.
echo This is the directory (relative to WORK_DIR) where uv creates the venv.
echo Default: .venv
echo.
set /p new_venv_dir="Enter new UV_VENV_DIR (press Enter to keep current): "

if "%new_venv_dir%"=="" (
    echo [INFO] No changes made
    timeout /t 1 >nul
    goto MainMenu
)

set "UV_VENV_DIR=%new_venv_dir%"
call :WriteConfig
echo.
echo [SUCCESS] UV_VENV_DIR updated to: %UV_VENV_DIR%
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
set "GITHUB_REPO=https://github.com/weihong-su/miniQMT"
set "ENV_TYPE=uv"
set "UV_VENV_DIR=.venv"
set "REQUIREMENTS_FILE=utils/requirements.txt"

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
        if "!line!"=="CONDA_ENV"         set "CONDA_ENV=!value!"
        if "!line!"=="WORK_DIR"          set "WORK_DIR=!value!"
        if "!line!"=="PYTHON_SCRIPT"     set "PYTHON_SCRIPT=!value!"
        if "!line!"=="SCRIPT_ARGS"       set "SCRIPT_ARGS=!value!"
        if "!line!"=="GITHUB_REPO"       set "GITHUB_REPO=!value!"
        if "!line!"=="ENV_TYPE"          set "ENV_TYPE=!value!"
        if "!line!"=="UV_VENV_DIR"       set "UV_VENV_DIR=!value!"
        if "!line!"=="REQUIREMENTS_FILE" set "REQUIREMENTS_FILE=!value!"
    )
)

:: Remove leading/trailing spaces
for /f "tokens=* delims= " %%a in ("%CONDA_ENV%")         do set "CONDA_ENV=%%a"
for /f "tokens=* delims= " %%a in ("%WORK_DIR%")          do set "WORK_DIR=%%a"
for /f "tokens=* delims= " %%a in ("%PYTHON_SCRIPT%")     do set "PYTHON_SCRIPT=%%a"
for /f "tokens=* delims= " %%a in ("%ENV_TYPE%")          do set "ENV_TYPE=%%a"
for /f "tokens=* delims= " %%a in ("%UV_VENV_DIR%")       do set "UV_VENV_DIR=%%a"
for /f "tokens=* delims= " %%a in ("%REQUIREMENTS_FILE%") do set "REQUIREMENTS_FILE=%%a"
for /f "tokens=* delims= " %%a in ("%GITHUB_REPO%")       do set "GITHUB_REPO=%%a"

goto :eof

:: ====================================
:: Write Configuration File
:: ====================================
:WriteConfig
(
    echo [Environment]
    echo # Environment type: conda or uv
    echo ENV_TYPE=%ENV_TYPE%
    echo.
    echo # Conda virtual environment name (used when ENV_TYPE=conda)
    echo CONDA_ENV=%CONDA_ENV%
    echo.
    echo # UV virtual environment directory (used when ENV_TYPE=uv)
    echo UV_VENV_DIR=%UV_VENV_DIR%
    echo.
    echo [Project]
    echo # Project working directory
    echo WORK_DIR=%WORK_DIR%
    echo.
    echo # GitHub repository URL (for cloning)
    echo GITHUB_REPO=%GITHUB_REPO%
    echo.
    echo # Path to requirements file (relative to WORK_DIR)
    echo REQUIREMENTS_FILE=%REQUIREMENTS_FILE%
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
