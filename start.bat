@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM start.bat - запуск сервера списания кредитов (FastAPI :4010).
REM
REM Запускает uvicorn для server.py в новом окне. Конфиг читается
REM из .env (CREDITS_API_KEY, ADMIN_KEY, FORWARD_URL, PORT и т.д.).
REM Идемпотентно: если порт уже занят - не запускает второй экземпляр.
REM ============================================================

set "PORT=4010"

REM --- defaults (перекрываются .env, если есть) ---
set "BASE_URL=http://localhost:4010"
set "DB_PATH=credits.db"
set "HOST=0.0.0.0"

REM --- загрузка .env (KEY=VALUE; комментарии # и кавычки режутся) ---
set "ENV_FILE=%~dp0.env"
if exist "%ENV_FILE%" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
        set "_k=%%A"
        set "_v=%%B"
        if "!_k!"=="PORT"        set "PORT=!_v!"
        if "!_k!"=="HOST"        set "HOST=!_v!"
        if "!_k!"=="BASE_URL"    set "BASE_URL=!_v!"
        if "!_k!"=="DB_PATH"     set "DB_PATH=!_v!"
    )
)

cd /d "%~dp0"
set "SRV_DIR=%CD%"

REM --- не запускать второй экземпляр, если порт уже слушает ---
netstat -ano | findstr LISTENING | findstr ":%PORT% " >nul 2>&1
if not errorlevel 1 (
    echo [INFO] порт %PORT% уже занят - сервер, видимо, уже запущен.
    pause
    exit /b 0
)

REM --- формируем команду запуска в зависимости от того, что доступно ---
set "RUN_CMD="

if exist "%SRV_DIR%\venv\Scripts\python.exe" (
    set "RUN_CMD="%SRV_DIR%\venv\Scripts\python.exe" -m uvicorn server:app --host %HOST% --port %PORT%"
)

if not defined RUN_CMD if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "RUN_CMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m uvicorn server:app --host %HOST% --port %PORT%"
)

if not defined RUN_CMD (
    python --version >nul 2>&1 && (
        set "RUN_CMD=python -m uvicorn server:app --host %HOST% --port %PORT%"
    )
)

if not defined RUN_CMD (
    py -3 --version >nul 2>&1 && (
        set "RUN_CMD=py -3 -m uvicorn server:app --host %HOST% --port %PORT%"
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

REM Запуск в новом окне.
REM ВАЖНО: первый аргумент start в кавычках = заголовок окна ("credits-server").
REM Саму команду запускаем через временный _run_tmp.bat, чтобы корректно
REM обработать случай с пробелом (py -3) и кавычками в пути к python.exe.
set "_RUNTMP=%TEMP%\credits_start_%RANDOM%.bat"
> "%_RUNTMP%" echo @echo off
>>"%_RUNTMP%" echo cd /d "%SRV_DIR%"
>>"%_RUNTMP%" echo !RUN_CMD!
>>"%_RUNTMP%" echo echo.
>>"%_RUNTMP%" echo echo Сервер остановлен. Нажмите любую клавишу для закрытия окна.
>>"%_RUNTMP%" echo pause >nul

start "credits-server" "%_RUNTMP%"

echo.
echo Окно сервера: "credits-server". GUI: http://localhost:%PORT%/admin
echo Остановить: stop.bat
timeout /t 3 >nul
exit /b 0
