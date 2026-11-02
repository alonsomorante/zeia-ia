"""Revisa que el bot pueda funcionar en este servidor, SIN mandar nada al grupo.

    sudo systemctl start zeia-bot-verificar && journalctl -u zeia-bot-verificar -n 30 -o cat

(instalar.sh lo corre solo al final, en una unidad con el mismo aislamiento que
el bot). Revisa: .env y llaves, que data/ se pueda
escribir, cada base por su túnel (una consulta de solo lectura: la última
lectura), el token de Telegram (getMe, no envía mensajes) y que Chromium pueda
dibujar una foto. Sale con código 1 si algo impide que el bot funcione; que una
base no tenga datos recientes es solo un aviso (puede ser una caída real).
"""
from __future__ import annotations

import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP))

LIMA = timezone(timedelta(hours=-5))
VIEJO = timedelta(minutes=30)
OBLIGATORIAS = ("ENERGIA_DB_PASSWORD", "AMBIENTAL_DB_PASSWORD",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
ULTIMA = "SELECT created_at FROM readings_reading ORDER BY id DESC LIMIT 1"

errores: list[str] = []


def ok(msg: str) -> None:
    print(f"  OK     {msg}")


def aviso(msg: str) -> None:
    print(f"  AVISO  {msg}")


def error(msg: str) -> None:
    print(f"  ERROR  {msg}")
    errores.append(msg)


def revisar_archivos(config) -> None:
    print("Archivos")
    if not (APP / ".env").is_file():
        error(f"falta {APP / '.env'}")
        return
    faltan = [k for k in OBLIGATORIAS if not os.getenv(k, "").strip()]
    if faltan:
        error(f".env sin valor en: {', '.join(faltan)}")
    else:
        ok(".env con base, túneles y Telegram")
    for cfg in config.DBS.values():
        if not cfg.use_ssh_tunnel:
            continue
        llave = Path(cfg.ssh_key)
        if not llave.is_file():
            error(f"[{cfg.label}] falta la llave {llave}")
        elif os.name == "posix" and stat.S_IMODE(llave.stat().st_mode) & 0o077:
            error(f"[{cfg.label}] {llave.name} debe tener permisos 600 (ssh la rechaza)")
        else:
            ok(f"[{cfg.label}] llave {llave.name}")
    data = APP / "data"
    try:
        data.mkdir(exist_ok=True)
        prueba = data / ".escritura"
        prueba.write_text("ok", encoding="utf-8")
        prueba.unlink()
        ok(f"{data} se puede escribir")
    except OSError as e:
        error(f"no se puede escribir en {data}: {e}")


def revisar_bases(config, run_query) -> None:
    print("Bases (por túnel, solo lectura)")
    for base, cfg in config.DBS.items():
        try:
            filas = run_query(ULTIMA, base=base, max_rows=1)["rows"]
        except Exception as e:  # noqa: BLE001
            error(f"[{cfg.label}] no conecta: {type(e).__name__}: {str(e)[:200]}")
            continue
        if not filas:
            aviso(f"[{cfg.label}] conecta, pero no tiene lecturas")
            continue
        ult = datetime.fromisoformat(str(filas[0][0]))
        edad = datetime.now(timezone.utc) - ult
        texto = f"[{cfg.label}] conecta; última lectura {ult.astimezone(LIMA):%d-%b %H:%M} (Lima)"
        if edad > VIEJO:
            aviso(f"{texto}: hace {edad.days} d {edad.seconds // 3600} h, sin datos recientes")
        else:
            ok(texto)


def revisar_telegram(telegram) -> None:
    print("Telegram (getMe: no manda mensajes)")
    try:
        yo = telegram.get_me()
        ok(f"token válido: @{yo.get('username', '?')}")
    except Exception as e:  # noqa: BLE001
        error(f"Telegram: {type(e).__name__}: {str(e)[:200]}")


def revisar_chromium(foto_salas) -> None:
    print("Chromium (fotos)")
    html = ('<html><body><div id="foto" style="width:320px;padding:10px">'
            'Prueba ZEIA ✓ áéíóú ñ 🟢🟠🔴</div></body></html>')
    try:
        png = foto_salas.png_de_html(html, alto_aprox=200)
        if png[:8] == b"\x89PNG\r\n\x1a\n":
            ok(f"dibuja fotos ({len(png) // 1024} KB de prueba)")
        else:
            error("Chromium no devolvió un PNG")
    except Exception as e:  # noqa: BLE001
        error(f"Chromium: {type(e).__name__}: {str(e)[:300]}")


def main() -> int:
    from src import config, foto_salas, telegram, tunnel
    from src.db import run_query

    print(f"Bot ZEIA en {APP} · Python {sys.version.split()[0]}")
    version = APP / "VERSION"
    if version.is_file():
        print(f"Versión del paquete: {version.read_text(encoding='utf-8').strip()}")
    revisar_archivos(config)
    try:
        revisar_bases(config, run_query)
    finally:
        tunnel.close_tunnel()
    revisar_telegram(telegram)
    revisar_chromium(foto_salas)
    if errores:
        print(f"\nNO está listo: {len(errores)} problema(s) arriba.")
        return 1
    print("\nListo: el bot puede funcionar en este servidor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
