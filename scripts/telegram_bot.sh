#!/bin/bash
# Bot de Telegram del equipo (/ambiental, /energia, /agua + avisos automáticos)
# leyendo PRODUCCIÓN en vivo por túnel SSH (energía → 127.0.0.1:55432,
# ambiental → 127.0.0.1:5435). Equivalente macOS/Linux de scripts\telegram_bot.cmd:
# no modifica .env, solo sobrescribe estas variables para este proceso.
# Una sola instancia por PC (candado 127.0.0.1:47831). En macOS mantiene la
# Mac despierta mientras corre (caffeinate): con la Mac dormida no hay avisos.
#
# Uso:
#   scripts/telegram_bot.sh           # en primer plano (log en logs/telegram_bot.*.log)
#   scripts/telegram_bot.sh --fondo   # en segundo plano: sigue aunque cierres la terminal
# Detener (cierra también sus túneles): pkill -INT -f scripts/telegram_bot.py
set -euo pipefail
cd "$(dirname "$0")/.."

export ENERGIA_DB_PORT=55432 USE_SSH_TUNNEL_ENERGIA=true
export AMBIENTAL_DB_PORT=5435 USE_SSH_TUNNEL_AMBIENTAL=true
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
mkdir -p logs

CMD=(venv/bin/python scripts/telegram_bot.py)
if command -v caffeinate >/dev/null 2>&1; then
    CMD=(caffeinate -is "${CMD[@]}")     # sin reposo del sistema mientras corra
fi

if [ "${1:-}" = "--fondo" ]; then
    set -m    # grupo de procesos propio: no muere con la terminal que lo lanzó
    nohup "${CMD[@]}" >> logs/telegram_bot.out.log 2>> logs/telegram_bot.err.log < /dev/null &
    PID=$!
    sleep 5
    if kill -0 "$PID" 2>/dev/null; then
        echo "Bot en segundo plano (pid $PID). Log: logs/telegram_bot.out.log"
    else
        echo "El bot no arrancó. Últimas líneas del log:" >&2
        tail -n 5 logs/telegram_bot.out.log logs/telegram_bot.err.log >&2
        exit 1
    fi
else
    exec "${CMD[@]}" >> logs/telegram_bot.out.log 2>> logs/telegram_bot.err.log
fi
