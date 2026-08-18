@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM start-dashboard.bat - запуск сервера списания кредитов И открытие
REM дашборда аналитики (/dashboard) в браузере.
REM
REM Аналог start-gui.bat, но открывает /dashboard вместо /admin.
REM Убивает старый экземпляр на порту (только LISTENING-сокеты,
REM чтобы не зацепить origin-соединения cloudflared), поднимает
REM uvicorn в отдельном окне "credits-server", ждёт /health
REM до 20 x 2 сек, затем открывает браузер.
REM ============================================================

set "PORT=4010"

REM --- defaults (перекрываются .env, если есть) ---
set "HOST=0.0.0.0"
set "BASE_URL=http://localhost:4010"

REM --- загрузка .env (KEY=VALUE; комментарии # и кавычки режутся) ---
set "ENV_FILE=%~dp0.env"
if exist "%ENV_FILE%" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
        set "_k=%%A"
        set "_v=%%B"
        if "!_k!"=="PORT"     set "PORT=!_v!"
        if "!_k!"=="HOST"     set "HOST=!_v!"
        if "!_k!"=="BASE_URL" set "BASE_URL=!_v!"
    )
)

cd /d "%~dp0"
set "SRV_DIR=%CD%"

REM --- остановить старый экземпляр: только LISTENING на нашем порту ---
REM (Get-NetTCPConnection -State Listen не цепляет origin-коннекты cloudflared;
REM  PowerShell отдаёт PID первым и единственным токеном каждой строки.)
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }" >nul 2>&1
if errorlevel 1 (
    echo [INFO] порт %PORT% занят - останавливаем старый экземпляр...
    set /a KILL_TRIES=10
) else (
    set /a KILL_TRIES=0
)

:killloop
if !KILL_TRIES! LEQ 0 goto killed
for /f "tokens=*" %%P in (
    'powershell -NoProfile -Command "(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue).OwningProcess | Sort-Object -Unique"'
) do taskkill /PID %%P /F >nul 2>&1
timeout /t 2 >nul
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }" >nul 2>&1
if not errorlevel 1 goto killed
set /a KILL_TRIES-=1
goto killloop

:killed
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }" >nul 2>&1
if errorlevel 1 echo [WARN] порт %PORT% всё ещё занят - новый экземпляр может не подняться.

REM --- формируем команду запуска в зависимости от того, что доступно ---
REM ВАЖНО: "python" может быть заглушкой Windows Store (exit 49, "Python was
REM not found"), поэтому сначала проверяем конкретные пути, и только потом PATH.
set "RUN_CMD="

if exist "%SRV_DIR%\venv\Scripts\python.exe" (
    set "RUN_CMD="%SRV_DIR%\venv\Scripts\python.exe" -m uvicorn server:app --host %HOST% --port %PORT%"
)

if not defined RUN_CMD if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "RUN_CMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m uvicorn server:app --host %HOST% --port %PORT%"
)

if not defined RUN_CMD if exist "C:\Windows\py.exe" (
    set "RUN_CMD=py -3 -m uvicorn server:app --host %HOST% --port %PORT%"
)

if not defined RUN_CMD (
    python -m uvicorn --version >nul 2>&1 && (
        set "RUN_CMD=python -m uvicorn server:app --host %HOST% --port %PORT%"
    )
)

if not defined RUN_CMD (
    echo [ERROR] python not found.
    echo         Установите Python или создайте venv\Scripts\python.exe рядом с start.bat.
    pause
    exit /b 1
)

echo Starting credits server on %HOST%:%PORT% ...
echo Dir: %SRV_DIR%
echo Run : !RUN_CMD!

REM --- отдельный временный батник: окно сервера живёт само по себе ---
set "_RUNTMP=%TEMP%\credits_start_dashboard_%RANDOM%.bat"
> "%_RUNTMP%" echo @echo off
>>"%_RUNTMP%" echo cd /d "%SRV_DIR%"
>>"%_RUNTMP%" echo !RUN_CMD!
>>"%_RUNTMP%" echo echo.
>>"%_RUNTMP%" echo echo Сервер остановлен. Нажмите любую клавишу для закрытия окна.
>>"%_RUNTMP%" echo pause ^>nul

start "credits-server" "%_RUNTMP%"

REM --- ждём /health до 20 x 2 сек (сервер может грузить БД при старте) ---
set /a TRIES=20
:waitloop
powershell -NoProfile -Command "try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://localhost:%PORT%/health' | Out-Null; exit 0}catch{exit 1}" >nul 2>&1
if not errorlevel 1 goto ready
set /a TRIES-=1
if %TRIES% LEQ 0 (
    echo [WARN] /health не ответил за 40 сек - открываем дашборд всё равно.
    goto open
)
timeout /t 2 >nul
goto waitloop

:ready
echo [OK] сервер поднялся на порту %PORT%.

:open
start "" "http://localhost:%PORT%/dashboard"

echo.
echo Окно сервера: "credits-server". Дашборд открывается в браузере.
echo Дашборд (локально, авто-логин): http://localhost:%PORT%/dashboard
echo Дашборд (публично, нужен ANALYTICS_API_KEY): %BASE_URL%/dashboard
echo Админка:  http://localhost:%PORT%/admin
echo Остановить: stop.bat
timeout /t 3 >nul
exit /b 0
