"""Arma el paquete del bot de Telegram para el servidor: SOLO lo que el bot usa.

    python deploy/bot/empaquetar.py          # dist/zeia-bot-<commit>.tar.gz
    python deploy/bot/empaquetar.py --env    # además dist/bot.env (con secretos)

El paquete sale de una lista cerrada de archivos (ARCHIVOS): lo que no está en
la lista no viaja, aunque se agregue al repositorio mañana. Así quedan fuera la
webapp, el chat con IA, la voz, las facturas en PDF, los backups y los docs.
Nunca lleva .env ni .pem: esos se copian aparte (ver deploy/bot/README.md).

--env arma dist/bot.env desde tu .env con SOLO las variables del bot (las de
deploy/bot/env.bot.example), sin claves de OpenRouter ni ElevenLabs. Host,
puerto y túnel de cada base salen del ejemplo (en el servidor siempre es por
túnel), aunque tu .env esté en modo "bases locales". dist/ está en .gitignore.
"""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AQUI = Path(__file__).resolve().parent
DIST = ROOT / "dist"
EJEMPLO_ENV = AQUI / "env.bot.example"

# Lo que necesita el bot (comprobado cargándolo: estos son todos los módulos
# propios que importa), las pruebas de esos módulos y los archivos de instalación.
ARCHIVOS = [
    "scripts/telegram_bot.py",
    "scripts/avisos.py",            # --ciclo / --estado para revisar en el servidor
    "src/__init__.py",
    "src/config.py",
    "src/db.py",
    "src/tunnel.py",
    "src/telegram.py",
    "src/monitor.py",
    "src/gateways.py",
    "src/foto_salas.py",
    "src/foto_cierre.py",
    "src/fotos_dia.py",
    "src/avisos.py",
    "src/avisos_datos.py",
    "src/avisos_vivo.py",
    "tests/test_avisos.py",
    "tests/test_avisos_datos.py",
    "tests/test_avisos_vivo.py",
    "tests/test_foto_cierre.py",
    "tests/test_fotos_dia.py",
    "tests/test_foto_salas.py",
    "tests/test_telegram.py",
    "tests/test_telegram_bot.py",
    "deploy/bot/requirements-bot.txt",
    "deploy/bot/env.bot.example",
    "deploy/bot/instalar.sh",
    "deploy/bot/zeia-bot.service",
    "deploy/bot/verificar.py",
    "deploy/bot/README.md",
]

PROHIBIDOS = (".env", ".pem", ".sqlite", ".pdf", ".key")   # nunca, aunque alguien los liste
OBLIGATORIAS = ("ENERGIA_DB_PASSWORD", "AMBIENTAL_DB_PASSWORD",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def version() -> str:
    """Commit corto; con '-local' si algún archivo del paquete tiene cambios sin commit."""
    commit = _git("rev-parse", "--short", "HEAD") or "sin-git"
    sucio = _git("status", "--porcelain", "--", *ARCHIVOS)
    return f"{commit}-local" if sucio else commit


def _validar_lista(archivos: list[str]) -> None:
    malos = [a for a in archivos if a.lower().endswith(PROHIBIDOS) or "/." in f"/{a}"]
    if malos:
        raise SystemExit(f"Archivos prohibidos en la lista: {', '.join(malos)}")
    faltan = [a for a in archivos if not (ROOT / a).is_file()]
    if faltan:
        raise SystemExit(f"No existen: {', '.join(faltan)}")


def _agregar(tar: tarfile.TarFile, nombre: str, datos: bytes, modo: int = 0o644) -> None:
    info = tarfile.TarInfo(nombre)
    info.size, info.mode, info.mtime = len(datos), modo, int(time.time())
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(datos))


def empaquetar(destino: Path, archivos: list[str] = ARCHIVOS, ver: str | None = None) -> Path:
    """Escribe el .tar.gz con los archivos (en LF: en Windows git los deja en CRLF
    y bash no corre un .sh con CRLF) y un archivo VERSION."""
    _validar_lista(archivos)
    ver = ver or version()
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destino, "w:gz") as tar:
        for a in archivos:
            datos = (ROOT / a).read_bytes().replace(b"\r\n", b"\n")
            _agregar(tar, a, datos, 0o755 if a.endswith(".sh") else 0o644)
        _agregar(tar, "VERSION", f"{ver} {time.strftime('%Y-%m-%d %H:%M')}\n".encode())
    return destino


def _leer_env(ruta: Path) -> dict[str, str]:
    """KEY=valor por línea (sin comentarios); el valor tal cual, sin comentario final."""
    out: dict[str, str] = {}
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        valor = valor.split(" #", 1)[0].strip()
        out[clave.strip()] = valor
    return out


def _forzada(clave: str) -> bool:
    """En el servidor las bases van siempre por túnel: host, puerto y túnel del ejemplo."""
    return clave.endswith(("_DB_HOST", "_DB_PORT")) or clave.startswith("USE_SSH_TUNNEL_")


def armar_env(origen: Path, ejemplo: Path = EJEMPLO_ENV) -> str:
    """Contenido del .env del servidor: las claves del ejemplo, con el valor de
    `origen` (menos las forzadas). Falla si falta un secreto obligatorio."""
    base, propio = _leer_env(ejemplo), _leer_env(origen)
    valores = {k: (v if _forzada(k) else propio.get(k, v)) for k, v in base.items()}
    faltan = [k for k in OBLIGATORIAS if not valores.get(k)]
    if faltan:
        raise SystemExit(f"Faltan en {origen.name}: {', '.join(faltan)}")
    lineas = ["# .env del bot en el servidor (deploy/bot/empaquetar.py --env)."
              " Contiene secretos: no lo subas a git."]
    lineas += [f"{k}={v}" for k, v in valores.items()]
    return "\n".join(lineas) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Paquete del bot de Telegram para el servidor.")
    ap.add_argument("--env", action="store_true",
                    help="además arma dist/bot.env desde tu .env (solo variables del bot)")
    args = ap.parse_args(argv)

    ver = version()
    paquete = empaquetar(DIST / f"zeia-bot-{ver}.tar.gz", ver=ver)
    print(f"Paquete: {paquete} ({len(ARCHIVOS)} archivos, versión {ver})")
    if ver.endswith("-local"):
        print("  Ojo: lleva cambios sin commit (la versión dice -local).")
    if args.env:
        destino = DIST / "bot.env"
        # write_bytes: siempre LF (write_text(newline=) no existe en Python 3.9)
        destino.write_bytes(armar_env(ROOT / ".env").encode("utf-8"))
        print(f"Env:     {destino} (con secretos: bórralo después de copiarlo al servidor)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
