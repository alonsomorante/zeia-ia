@echo off
rem Registra (o reemplaza) la tarea programada que al iniciar sesion levanta
rem el bot de Telegram y la webapp en vivo, ocultos. No necesita administrador.
rem Probar ahora:   schtasks /run /tn "ZEIA-IA autoarranque"
rem Quitar:         schtasks /delete /tn "ZEIA-IA autoarranque" /f
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar_autoarranque.ps1"
if errorlevel 1 (
  echo No se pudo registrar la tarea "ZEIA-IA autoarranque".
  exit /b 1
)
endlocal
