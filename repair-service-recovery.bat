@echo off
setlocal enabledelayedexpansion

set "SERVICE_NAME=LecooControlDaemon"
set "SERVICE_FAILURE_RESET=86400"
set "HEADLESS=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--headless" set "HEADLESS=1"
if /I "%~1"=="/headless" set "HEADLESS=1"
if /I "%~1"=="/silent" set "HEADLESS=1"
shift
goto parse_args
:args_done

net session >nul 2>&1
if !errorLevel! neq 0 (
    if "!HEADLESS!"=="1" (
        powershell -NoProfile -WindowStyle Hidden -Command "try { $q=[char]34; $arg='/d /c call ' + $q + '%~f0' + $q + ' --headless'; $p=Start-Process -FilePath cmd.exe -ArgumentList $arg -Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode } catch { exit 1 }"
        exit /b !errorLevel!
    )
    powershell -NoProfile -Command "try { Start-Process -FilePath cmd.exe -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs -Wait } catch { Write-Host 'UAC cancelled.'; pause }"
    exit /b
)

echo.
echo ============================================================
echo   Lecoo Control Center - Service Recovery Repair
echo ============================================================
echo   Service : %SERVICE_NAME%
echo ============================================================
echo.

sc query "%SERVICE_NAME%" >nul 2>&1
if !errorLevel! neq 0 (
    echo [FAIL] Service not found: %SERVICE_NAME%
    echo        Run install.bat as administrator first.
    goto :done
)

echo [1/3] Ensuring automatic startup...
sc config "%SERVICE_NAME%" start= auto >nul 2>&1
if !errorLevel! neq 0 (
    echo       [WARN] Could not set automatic startup.
) else (
    echo       [OK] Startup type is automatic.
)
echo.

echo [2/3] Configuring failure recovery...
sc failure "%SERVICE_NAME%" reset= %SERVICE_FAILURE_RESET% actions= restart/5000/restart/10000/restart/30000 >nul 2>&1
if !errorLevel! neq 0 (
    echo       [FAIL] Could not configure service recovery.
    goto :done
)
sc failureflag "%SERVICE_NAME%" 1 >nul 2>&1
echo       [OK] Service will restart automatically after failures.
echo.

echo [3/3] Checking service state...
sc query "%SERVICE_NAME%" | find "RUNNING" >nul 2>&1
if !errorLevel! equ 0 (
    echo       [OK] Service is running.
) else (
    echo       Service is not running. Starting...
    sc start "%SERVICE_NAME%" >nul 2>&1
    if !errorLevel! neq 0 (
        echo       [WARN] Could not start service. Check Windows Services.
    ) else (
        echo       [OK] Service started.
    )
)
echo.

echo Current recovery policy:
sc qfailure "%SERVICE_NAME%"

:done
echo.
if "!HEADLESS!"=="0" pause
endlocal
