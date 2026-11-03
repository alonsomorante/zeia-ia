#!/usr/bin/env bash
# Sube el código actual del bot a la EC2 y lo instala, en un solo comando (Mac/Linux).
#
#   bash deploy/bot/actualizar.sh
#
# Hace los 3 pasos de "Actualizar el código" de deploy/bot/README.md:
#   1) arma el paquete (deploy/bot/empaquetar.py, sin --env: el .env del servidor no se toca)
#   2) lo copia al servidor (scp)
#   3) lo instala (instalar.sh: prueba el código nuevo, y si algo falla deja el anterior)
#
# Servidor y llave por defecto; se cambian con variables de entorno:
#   ZEIA_BOT_HOST=ubuntu@1.2.3.4 ZEIA_BOT_KEY=~/.ssh/otra.pem bash deploy/bot/actualizar.sh
set -euo pipefail

HOST="${ZEIA_BOT_HOST:-ubuntu@23.23.69.185}"
KEY="${ZEIA_BOT_KEY:-$HOME/.ssh/zeia-bot-cobertura.pem}"
RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$RAIZ"

falla() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$KEY" ]] || falla "no encuentro la llave $KEY (ZEIA_BOT_KEY=... para otra ruta)"
PY="$RAIZ/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)" || falla "no hay Python (ni venv/ ni python3)"
SSH=(ssh -i "$KEY" -o ConnectTimeout=15 -o BatchMode=yes)

echo "==> 1/3 Armando el paquete"
SALIDA="$("$PY" deploy/bot/empaquetar.py)"
echo "$SALIDA"
PAQUETE="$(printf '%s\n' "$SALIDA" | sed -n 's/^Paquete: \(.*\.tar\.gz\) (.*/\1/p')"
[[ -f "$PAQUETE" ]] || falla "no se armó el paquete"
NOMBRE="$(basename "$PAQUETE")"
if [[ "$NOMBRE" == *-local.tar.gz ]]; then
    read -r -p "El paquete lleva cambios sin commit. ¿Subirlo igual? [s/N] " r
    [[ "$r" =~ ^[sS]$ ]] || falla "cancelado: haz commit y vuelve a correr"
fi

echo "==> 2/3 Copiando $NOMBRE a $HOST"
scp -q -i "$KEY" -o ConnectTimeout=15 -o BatchMode=yes "$PAQUETE" "$HOST:~/"

echo "==> 3/3 Instalando en el servidor"
# Carpeta temporal para el instalador; el .tar.gz se borra si la instalación salió bien.
"${SSH[@]}" "$HOST" "set -e; d=\$(mktemp -d); trap 'rm -rf \"\$d\"' EXIT
tar -xzf ~/'$NOMBRE' -C \"\$d\" deploy/bot/instalar.sh
sudo bash \"\$d/deploy/bot/instalar.sh\" ~/'$NOMBRE'
rm -f ~/'$NOMBRE'
echo; echo \"Versión instalada: \$(cat /opt/zeia-bot/app/VERSION)\"
echo \"Bot: \$(systemctl is-active zeia-bot || true)\""
