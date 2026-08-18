@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM start-system-credits.bat - FULL START of the credits server:
REM   window 1 "credits-server" : backend  (FastAPI server:app :4010)
REM   window 2 "credits-tunnel" : cloudflared (credits.dev-bim.com)
REM
REM Registered in the START-servers launcher set as slug "credits"
REM (start-one.bat credits calls THIS file - do not rename).
REM
REM ASCII-only on purpose: cmd.exe on ru-RU reads .bat in OEM
REM codepage; Cyrillic text lives in servers.py / README.md.
REM CRLF line endings required.
REM ============================================================

REM --- per-service settings (PORT overridden by .env) ---
set "PORT=4010"
set "HOST=0.0.0.0"
set "TUNNEL_NAME=credits"
set "TUNNEL_CONFIG=config-credits.yml"
set "TUNNEL_UUID=db3069c5-b8e7-491d-a433-b78f96c3ae57"
set "PUBLIC_HOST=credits.dev-bim.com"

cd /d "%~dp0"
set "SRV_DIR=%CD%"

REM --- load .env overrides (PORT/HOST; comments and quotes stripped) ---
set "ENV_FILE=%~dp0.env"
if exist "%ENV_FILE%" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
        if "%%A"=="PORT" set "PORT=%%B"
        if "%%A"=="HOST" set "HOST=%%B"
    )
)

REM --- idempotent: port already listened -> skip backend window ---
netstat -ano | findstr LISTENING | findstr ":%PORT% " >nul 2>&1
if not errorlevel 1 (
    echo [INFO] port %PORT% already listened - backend window skipped.
    goto tunnel
)

REM --- find python: venv -> PATH python -> py -3 ---
set "RUN_CMD="
if exist "%SRV_DIR%\venv\Scripts\python.exe" set "RUN_CMD="%SRV_DIR%\venv\Scripts\python.exe" -m uvicorn server:app --host %HOST% --port %PORT%"
if not defined RUN_CMD (
    python --version >nul 2>&1 && set "RUN_CMD=python -m uvicorn server:app --host %HOST% --port %PORT%"
)
if not defined RUN_CMD (
    py -3 --version >nul 2>&1 && set "RUN_CMD=py -3 -m uvicorn server:app --host %HOST% --port %PORT%"
)
if not defined RUN_CMD (
    echo [ERROR] python not found.
    echo         Install Python or create venv\Scripts\python.exe next to this bat.
    pause
    exit /b 1
)

REM --- window 1: backend (temp bat = safe quoting for paths with spaces) ---
set "_RUNTMP=%TEMP%\credits_sys_%RANDOM%.bat"
> "%_RUNTMP%" echo @echo off
>>"%_RUNTMP%" echo cd /d "%SRV_DIR%"
>>"%_RUNTMP%" echo !RUN_CMD!
>>"%_RUNTMP%" echo echo.
>>"%_RUNTMP%" echo echo Backend stopped. Press any key to close this window.
>>"%_RUNTMP%" echo pause ^>nul
start "credits-server" "%_RUNTMP%"

REM --- wait for backend /health (up to 25 x 3s; /health is public) ---
set /a TRIES=0
:wait_backend
timeout /t 3 /nobreak >nul
set /a TRIES+=1
curl -s -m 3 -o nul "http://127.0.0.1:%PORT%/health" >nul 2>nul
if errorlevel 1 (
    if !TRIES! LSS 25 ( echo ...backend not ready, attempt !TRIES!/25 & goto wait_backend )
    echo [WARN] backend not ready after !TRIES! attempts - starting tunnel anyway.
) else (
    echo backend ready on :%PORT%
)

:tunnel
REM --- find cloudflared ---
REM NOT via "where": C:\Windows\System32\cloudflared.exe is a 0-byte stub
REM that fails with "Access denied". Use Program Files copy first.
set "CFEXE="
if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CFEXE=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if not defined CFEXE if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CFEXE=%ProgramFiles%\cloudflared\cloudflared.exe"
if not defined CFEXE if exist "%USERPROFILE%\.cloudflared\cloudflared.exe" set "CFEXE=%USERPROFILE%\.cloudflared\cloudflared.exe"
if not defined CFEXE (
    echo [ERROR] cloudflared not found.
    pause
    exit /b 1
)

set "CF_DIR=%USERPROFILE%\.cloudflared"
if not exist "%CF_DIR%\%TUNNEL_CONFIG%" (
    echo [ERROR] tunnel config missing: %CF_DIR%\%TUNNEL_CONFIG%
    pause
    exit /b 1
)
if not exist "%CF_DIR%\%TUNNEL_UUID%.json" (
    echo [ERROR] tunnel creds missing: %CF_DIR%\%TUNNEL_UUID%.json
    pause
    exit /b 1
)

REM --- window 2: tunnel (--config goes BEFORE run) ---
start "credits-tunnel" cmd /k "cd /d "%CF_DIR%" && "%CFEXE%" tunnel --config %TUNNEL_CONFIG% run %TUNNEL_NAME%"

echo.
echo Public : https://%PUBLIC_HOST%   (health: /health, GUI: /admin)
echo Windows: "credits-server" + "credits-tunnel"
echo Stop   : stop.bat ^(backend^) + close "credits-tunnel" window ^(or stop-all.bat^)
echo This window can be closed.
pause
endlocal
