@echo off
rem Reporte MANUAL de alertas y notificaciones por correo (energia, agua y
rem ambiental) en PDF. Lee PRODUCCION en vivo por tunel SSH (energia ->
rem 127.0.0.1:55432, ambiental -> 127.0.0.1:5435). No modifica .env: solo
rem sobrescribe estas variables para este proceso.
rem
rem Uso:
rem   scripts\reporte_alertas.cmd                          (semana pasada)
rem   scripts\reporte_alertas.cmd --desde 2026-11-02 --hasta 2026-11-08
rem   scripts\reporte_alertas.cmd --desde 2026-10-01 --hasta 2026-10-31 --detalle
cd /d "%~dp0.."
set ENERGIA_DB_PORT=55432
set USE_SSH_TUNNEL_ENERGIA=true
set AMBIENTAL_DB_PORT=5435
set USE_SSH_TUNNEL_AMBIENTAL=true
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PATH=C:\Windows\System32\OpenSSH;%PATH%
venv\Scripts\python.exe scripts\reporte_alertas.py %*
