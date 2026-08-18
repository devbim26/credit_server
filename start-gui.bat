@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM start-gui.bat - запуск сервера списания кредитов И открытие
REM GUI (админки) в браузере по умолчанию.
REM
REM Делает то же, что start.bat (поднимает uvicorn для server.py),
REM плюс через ~3 сек после старта открывает http://localhost:PORT/admin
REM в браузере. Конфиг порта/хоста читается из .env.
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
    echo        Открываем GUI в браузере...
    start "" "http://localhost:%PORT%/admin"
    timeout /t 2 >nul
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

REM --- отдельный временный батник, который:
REM   1) запускает uvicorn (сервер),
REM   2) ждёт, пока он поднимется (~3 сек),
REM   3) открывает GUI в браузере.
REM Всё в одном окне "credits-server", чтобы по нему было видно лог сервера.
set "_RUNTMP=%TEMP%\credits_start_gui_%RANDOM%.bat"
> "%_RUNTMP%" echo @echo off
>>"%_RUNTMP%" echo cd /d "%SRV_DIR%"
>>"%_RUNTMP%" echo start "" "http://localhost:%PORT%/admin"
>>"%_RUNTMP%" echo !RUN_CMD!
>>"%_RUNTMP%" echo echo.
>>"%_RUNTMP%" echo echo Сервер остановлен. Нажмите любую клавишу для закрытия окна.
>>"%_RUNTMP%" echo pause ^>nul

REM Запуск в новом окне. start "" открывает браузер ДО uvicorn (т.к. браузер
REM грузится дольше) - к моменту запроса сервер уже поднимется. Если браузер
REM откроется чуть раньше, страница сама повторит запросы к API.

start "credits-server" "%_RUNTMP%"

echo.
echo Окно сервера: "credits-server". GUI открывается в браузере.
echo GUI: http://localhost:%PORT%/admin
echo Остановить: stop.bat
timeout /t 3 >nul
exit /b 0
