@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM stop.bat — остановить сервер списания кредитов (порт :4010).
REM Спутник start.bat. Идемпотентно: ничего не падает, если уже не запущен.
REM ============================================================

REM --- порт берём из .env, иначе 4010 ---
set "PORT=4010"
set "ENV_FILE=%~dp0.env"
if exist "%ENV_FILE%" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
        if "%%A"=="PORT" set "PORT=%%B"
    )
)

echo Stopping credits server on :%PORT% ...

REM --- PID, слушающий порт ---
set "BE_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr LISTENING ^| findstr ":%PORT% "') do (
    if not "%%P"=="0" if not defined BE_PID set "BE_PID=%%P"
)

if not defined BE_PID (
    echo not running.
    exit /b 0
)

echo killing PID %BE_PID% ...
taskkill /F /PID %BE_PID% >nul 2>&1
if errorlevel 1 (
    echo [WARN] не удалось завершить PID %BE_PID%
    exit /b 1
)

echo stopped.
exit /b 0
