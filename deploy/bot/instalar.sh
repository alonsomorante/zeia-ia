#!/usr/bin/env bash
# Instala o actualiza el bot de Telegram de ZEIA en Ubuntu (22.04 / 24.04).
#
#   sudo bash instalar.sh zeia-bot-<version>.tar.gz
#
# Primera vez: crea el usuario zeiabot, /opt/zeia-bot, el entorno de Python,
# Chromium y el servicio, pero NO enciende el bot (primero hay que apagar el de
# la laptop: dos bots a la vez chocan en Telegram). Si ya está corriendo, lo
# actualiza y lo reinicia. Nunca toca .env, las llaves .pem ni data/.
# Las pruebas corren ANTES de reemplazar el código, y si la verificación falla
# se restaura el código anterior: un paquete malo no deja al bot roto.
#
# Estructura:
#   /opt/zeia-bot/app        código (de root, solo lectura para el bot)
#   /opt/zeia-bot/app/.env   secretos del bot (zeiabot, 600)
#   /opt/zeia-bot/app/*.pem  llaves de los túneles (zeiabot, 600)
#   /opt/zeia-bot/app/data   estado de avisos y foto de cierre (zeiabot, 700)
#   /opt/zeia-bot/anterior   copia del código previo (para volver atrás)
#   /opt/zeia-bot/venv       Python con las 6 librerías del bot
#   /opt/zeia-bot/browsers   Chromium de Playwright
#   /opt/zeia-bot/home       HOME del bot (known_hosts de ssh, caché)
set -euo pipefail

PAQUETE="${1:-}"
BASE=/opt/zeia-bot
APP="$BASE/app"
ANTERIOR="$BASE/anterior"
VENV="$BASE/venv"
BROWSERS="$BASE/browsers"
HOGAR="$BASE/home"
USUARIO=zeiabot
SERVICIO=zeia-bot
UNIDADES=/etc/systemd/system
CODIGO=(src scripts tests deploy VERSION)

paso() { printf '\n==> %s\n' "$*"; }
falla() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || falla "córrelo con sudo: sudo bash $0 <paquete.tar.gz>"
[[ -n "$PAQUETE" && -f "$PAQUETE" ]] || falla "uso: sudo bash $0 zeia-bot-<version>.tar.gz"
if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    [[ "${ID:-}" == ubuntu ]] || echo "Aviso: probado en Ubuntu; este sistema es ${PRETTY_NAME:-desconocido}."
fi

paso "Revisando el paquete"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
NUEVO="$TMP/nuevo"
mkdir "$NUEVO"
tar -xzf "$PAQUETE" -C "$NUEVO"
for f in VERSION scripts/telegram_bot.py src/config.py deploy/bot/requirements-bot.txt \
         deploy/bot/zeia-bot.service deploy/bot/verificar.py; do
    [[ -f "$NUEVO/$f" ]] || falla "el paquete no trae $f"
done
if find "$NUEVO" \( -name '.env' -o -name '*.pem' \) | grep -q .; then
    falla "el paquete trae secretos (.env o .pem): ármalo con deploy/bot/empaquetar.py"
fi
echo "Versión nueva: $(cat "$NUEVO/VERSION")"
[[ -f "$APP/VERSION" ]] && echo "Versión actual: $(cat "$APP/VERSION")"

paso "Paquetes del sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv openssh-client ca-certificates \
    fonts-roboto fonts-liberation fonts-noto-color-emoji >/dev/null

paso "Usuario $USUARIO y carpetas"
if ! id -u "$USUARIO" >/dev/null 2>&1; then
    useradd --system --home-dir "$HOGAR" --shell /usr/sbin/nologin "$USUARIO"
fi
install -d -o root -g root -m 755 "$BASE" "$APP"
install -d -o "$USUARIO" -g "$USUARIO" -m 700 "$HOGAR" "$APP/data"
chown -R "$USUARIO:$USUARIO" "$APP/data"     # incluye el estado copiado de la laptop

paso "Python ($(python3 --version)) y librerías"
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$NUEVO/deploy/bot/requirements-bot.txt"

paso "Chromium (Playwright)"
PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" "$VENV/bin/python" -m playwright install --with-deps chromium >/dev/null
chmod -R a+rX "$BROWSERS"

paso "Pruebas del código nuevo (sin base ni Telegram), antes de instalarlo"
( cd "$NUEVO" && PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -m unittest discover -s tests -q ) \
    || falla "las pruebas fallaron: no se cambió nada del bot"

paso "Código"
HAY_ANTERIOR=0
if [[ -f "$APP/VERSION" ]]; then
    rm -rf "$ANTERIOR"
    install -d -m 755 "$ANTERIOR"
    for d in "${CODIGO[@]}"; do
        [[ -e "$APP/$d" ]] && cp -a "$APP/$d" "$ANTERIOR/$d"
    done
    HAY_ANTERIOR=1
fi
instalar_codigo() {   # $1 = carpeta de origen
    for d in "${CODIGO[@]}"; do
        rm -rf "${APP:?}/$d"
        [[ -e "$1/$d" ]] && cp -a "$1/$d" "$APP/$d"
    done
    chown -R root:root "${CODIGO[@]/#/$APP/}"
    chmod -R u=rwX,go=rX "${CODIGO[@]/#/$APP/}"
}
instalar_codigo "$NUEVO"

paso "Secretos (.env y llaves)"
LISTO=1
if [[ ! -f "$APP/.env" ]]; then
    LISTO=0
    echo "Falta $APP/.env: cópialo (dist/bot.env de la laptop) y vuelve a correr este script."
fi
for llave in $(grep -hoE '^SSH_KEY_[A-Z]+=.+' "$APP/.env" 2>/dev/null | cut -d= -f2-); do
    if [[ ! -f "$APP/$llave" ]]; then
        LISTO=0
        echo "Falta la llave $APP/$llave"
    fi
done
for f in "$APP/.env" "$APP"/*.pem; do
    [[ -f "$f" ]] || continue
    chown "$USUARIO:$USUARIO" "$f"
    chmod 600 "$f"
done

paso "Servicio $SERVICIO"
instalar_unidades() {
    install -m 644 "$APP/deploy/bot/zeia-bot.service" "$UNIDADES/$SERVICIO.service"
    # Verificación: el MISMO aislamiento que el bot, pero corre verificar.py una vez
    sed -e 's#^Description=.*#Description=ZEIA - verificación del bot (no manda mensajes)#' \
        -e 's#^Type=simple#Type=oneshot#' \
        -e 's#^ExecStart=.*#ExecStart=/opt/zeia-bot/venv/bin/python deploy/bot/verificar.py#' \
        -e 's#^TimeoutStopSec=.*#TimeoutStartSec=180#' \
        -e '/^Restart/d' -e '/^StartLimitIntervalSec/d' -e '/^\[Install\]/,$d' \
        "$APP/deploy/bot/zeia-bot.service" > "$UNIDADES/$SERVICIO-verificar.service"
    systemctl daemon-reload
}
instalar_unidades

if [[ $LISTO -eq 0 ]]; then
    paso "Código instalado, pero falta configurar (arriba). El bot NO está encendido."
    exit 0
fi

paso "Verificación (con el mismo aislamiento que el bot)"
INICIO="$(date '+%Y-%m-%d %H:%M:%S')"
if ! systemctl start "$SERVICIO-verificar"; then
    journalctl -u "$SERVICIO-verificar" --since "$INICIO" --no-pager -o cat || true
    if [[ $HAY_ANTERIOR -eq 1 ]]; then
        instalar_codigo "$ANTERIOR"
        instalar_unidades
        falla "la verificación falló (arriba): se restauró el código anterior ($(cat "$APP/VERSION"))"
    fi
    falla "la verificación falló (arriba): el bot no se encendió"
fi
journalctl -u "$SERVICIO-verificar" --since "$INICIO" --no-pager -o cat || true

if systemctl is-active --quiet "$SERVICIO"; then
    systemctl restart "$SERVICIO"
    paso "Actualizado y reiniciado. Log: journalctl -u $SERVICIO -f"
else
    # No se habilita solo: si el servidor se reinicia antes de apagar el de la
    # laptop, habría dos bots a la vez.
    paso "Listo, pero APAGADO a propósito."
    echo "1) Apaga el bot de la laptop y su autoarranque (ver deploy/bot/README.md)."
    echo "2) Enciéndelo aquí (y que arranque solo al prender): sudo systemctl enable --now $SERVICIO"
    echo "3) Míralo:  journalctl -u $SERVICIO -f"
fi
