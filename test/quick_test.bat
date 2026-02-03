@echo off
REM miniQMT Regression Test Suite - Quick Start Script
REM Run all regression tests with a single click

echo ========================================
echo miniQMT Regression Test Suite
echo ========================================
echo.

REM Set Python executable path (Anaconda environment)
set PYTHON_EXE=C:\Users\PC\Anaconda3\envs\python39\python.exe

REM Check if Python exists
if not exist "%PYTHON_EXE%" (
    echo ERROR: Python not found at: %PYTHON_EXE%
    echo.
    echo Please update the PYTHON_EXE path in quick_test.bat
    echo or install Python 3.9 environment
    pause
    exit /b 1
)

echo Using Python: %PYTHON_EXE%
echo.

REM Change to project root
cd /d %~dp0..

REM Run tests
echo Running regression tests...
echo.
"%PYTHON_EXE%" test/run_all_tests.py

REM Store exit code
set TEST_EXIT_CODE=%ERRORLEVEL%

echo.
echo ========================================
if %TEST_EXIT_CODE%==0 (
    echo TEST RESULT: ALL TESTS PASSED
) else (
    echo TEST RESULT: SOME TESTS FAILED
)
echo ========================================
echo.

REM Check if test report was generated
for /f %%i in ('dir /b /o-d test\reports\test_report_*.json 2^>nul') do (
    echo Latest report: test\reports\%%i
    goto :report_found
)
echo No test report generated
:report_found

echo.
echo Press any key to exit...
pause > nul

exit /b %TEST_EXIT_CODE%
