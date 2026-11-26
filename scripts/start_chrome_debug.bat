@echo off
echo Iniciando Google Chrome con puerto de depuracion 9222 sobre perfil dedicado...
set "DEBUG_DIR=%USERPROFILE%\ChromeDebug"
if not "%JOB_AUTOMATOR_CHROME_DEBUG_DIR%"=="" set "DEBUG_DIR=%JOB_AUTOMATOR_CHROME_DEBUG_DIR%"
set "CDP_PORT=9222"
if not "%JOB_AUTOMATOR_CDP_PORT%"=="" set "CDP_PORT=%JOB_AUTOMATOR_CDP_PORT%"

if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=%CDP_PORT% --user-data-dir="%DEBUG_DIR%" --no-first-run --no-default-browser-check
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" --remote-debugging-port=%CDP_PORT% --user-data-dir="%DEBUG_DIR%" --no-first-run --no-default-browser-check
) else (
    echo No se encontro chrome.exe en Program Files. Ajusta la ruta en este script.
    exit /b 1
)
