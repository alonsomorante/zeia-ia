"""Foto de cierre del día: lo que tuvo problemas en TODO el día que terminó.

Todos los días a la hora FOTO_CIERRE (.env; 00:10 por defecto, hora Lima) el
bot de Telegram manda al grupo, SIN sonido, la foto de cada módulo (ambiental,
energía y agua) del día anterior completo, de 00:00 a 24:00: las mismas fotos
de /ambiental, /energia y /agua (src/foto_salas.py), pero del día entero. Un
módulo sin problemas manda solo el texto "✅ Todo bien".

- Si el bot estaba apagado a esa hora (PC apagada o suspendida), las manda
  apenas vuelve, y solo las del último día terminado (no acumula días).
- Lo enviado se anota en data/foto_cierre.json: un reinicio no repite nada.
- Si un módulo falla (base o Telegram caídos) se reintenta cada REINTENTO_MIN
  minutos; a los MAX_INTENTOS se avisa en el grupo y se deja ese día (si ni
  el aviso llega, sigue reintentando hasta que toque el cierre siguiente).
- FOTO_CIERRE=apagado lo desactiva. No depende de AVISOS_MODO.

Lo revisa el bucle principal del bot (scripts/telegram_bot.py) cada vez que
vuelve de consultar a Telegram (≤ 50 s): sin hilos ni otro proceso.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import config
from .avisos import fecha_larga

LIMA = timezone(timedelta(hours=-5))
MODULOS = ("ambiental", "energia", "agua")     # el mismo orden que los comandos
HORA_DEFECTO = 10                              # 00:10
APAGADO = {"apagado", "apagada", "off", "no", "false", "nunca"}
REINTENTO_MIN = 10
MAX_INTENTOS = 4                               # ~30 min de reintentos
RUTA = config.ROOT / "data" / "foto_cierre.json"


def hora_de(texto: Optional[str], defecto: int = HORA_DEFECTO) -> Optional[int]:
    """'00:10' → 10 (minuto del día); 'apagado' → None; si no se entiende, `defecto`."""
    s = str(texto or "").strip().lower()
    if s in APAGADO:
        return None
    try:
        h, m = (int(x) for x in s.split(":"))
    except ValueError:
        return defecto
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else defecto


def texto_hora(minuto: int) -> str:
    return f"{minuto // 60:02d}:{minuto % 60:02d}"


def dia_debido(t: datetime, hora_min: int) -> date:
    """El último día terminado cuya foto de cierre ya tocó a la hora `t`."""
    tl = t.astimezone(LIMA)
    return tl.date() - timedelta(days=1 if tl.hour * 60 + tl.minute >= hora_min else 2)


def _corto(ex: Exception) -> str:
    txt = (str(ex).strip().splitlines() or [type(ex).__name__])[0]
    return txt[:200]


class Cierre:
    """Manda una vez por día las fotos del día que terminó (lo llama el bucle del bot).

    enviar(modulo, dia): arma y manda la foto de un módulo; lanza si falla.
    avisar(texto): manda un texto al grupo (cuando un módulo se da por perdido).
    """

    def __init__(self, enviar: Callable[[str, date], None], avisar: Callable[[str], None],
                 hora: Optional[str] = None, ruta: Optional[Path] = None,
                 ahora: Optional[Callable[[], datetime]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.hora_min = hora_de(config.FOTO_CIERRE if hora is None else hora)
        self.enviar = enviar
        self.avisar = avisar
        self.ruta = Path(ruta) if ruta is not None else RUTA
        self.ahora = ahora or (lambda: datetime.now(timezone.utc))
        self.log = log or (lambda m: print(m, flush=True))
        self._hechos: dict = {}        # día → módulos ya resueltos (por si no se puede anotar)
        self._fallos: dict = {}        # (día, módulo) → (intentos, próximo intento)

    def descripcion(self) -> str:
        if self.hora_min is None:
            return "Foto de cierre apagada (FOTO_CIERRE=apagado)"
        return (f"Foto de cierre: a las {texto_hora(self.hora_min)} manda el día anterior "
                f"completo, sin sonido (registro en {self.ruta})")

    def _leer(self) -> dict:
        try:
            datos = json.loads(self.ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return datos if isinstance(datos, dict) else {}

    def _guardar(self, dia: date, hechos: list) -> None:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ruta.with_name(self.ruta.name + ".tmp")
        tmp.write_text(json.dumps({"dia": dia.isoformat(), "hechos": hechos}), encoding="utf-8")
        os.replace(tmp, self.ruta)     # atómico: un corte no deja el archivo a medias

    def pendientes(self, t: Optional[datetime] = None) -> tuple:
        """(día, módulos que faltan mandar); (None, []) si no toca nada."""
        if self.hora_min is None:
            return None, []
        dia = dia_debido(t or self.ahora(), self.hora_min)
        guardado = self._leer()
        ultimo = str(guardado.get("dia") or "")
        if ultimo > dia.isoformat():
            return None, []            # ya hay uno más nuevo (el reloj volvió atrás)
        hechos = set(self._hechos.get(dia, ()))
        if ultimo == dia.isoformat():
            hechos |= set(guardado.get("hechos") or [])
        faltan = [m for m in MODULOS if m not in hechos]
        return (dia, faltan) if faltan else (None, [])

    def revisar(self) -> list:
        """Manda lo que falte del último cierre; devuelve los módulos enviados. No lanza."""
        try:
            return self._revisar()
        except Exception as ex:  # noqa: BLE001 - el bot no debe caerse por la foto de cierre
            self.log(f"Cierre: no pude revisar: {type(ex).__name__}: {_corto(ex)}")
            return []

    def _revisar(self) -> list:
        t = self.ahora()
        dia, faltan = self.pendientes(t)
        if dia is None:
            return []
        self._hechos = {dia: {m for m in MODULOS if m not in faltan}}
        self._fallos = {k: v for k, v in self._fallos.items() if k[0] == dia}
        enviados = []
        for modulo in faltan:
            intentos, proximo = self._fallos.get((dia, modulo), (0, None))
            if proximo is not None and t < proximo:
                continue
            try:
                self.enviar(modulo, dia)
            except Exception as ex:  # noqa: BLE001 - se reintenta o se avisa en el grupo
                intentos += 1
                # Se da por perdido solo si se pudo avisar; si Telegram tampoco
                # responde, se sigue reintentando (puede volver todo junto).
                if intentos < MAX_INTENTOS or not self._avisar_fallo(modulo, dia, ex):
                    self._fallos[(dia, modulo)] = (intentos, t + timedelta(minutes=REINTENTO_MIN))
                    self.log(f"Cierre del {dia.isoformat()}: /{modulo} falló ({_corto(ex)}); "
                             f"reintento en {REINTENTO_MIN} min")
                    continue
                self.log(f"Cierre del {dia.isoformat()}: /{modulo} falló {intentos} veces "
                         f"({_corto(ex)}); avisé en el grupo y no lo intento más")
            else:
                enviados.append(modulo)
            self._fallos.pop((dia, modulo), None)
            self._anotar(dia, modulo)
        return enviados

    def _anotar(self, dia: date, modulo: str) -> None:
        hechos = self._hechos.setdefault(dia, set())
        hechos.add(modulo)
        try:
            self._guardar(dia, [m for m in MODULOS if m in hechos])
        except OSError as ex:
            self.log(f"Cierre: no pude anotar lo enviado en {self.ruta}: {_corto(ex)}")

    def _avisar_fallo(self, modulo: str, dia: date, ex: Exception) -> bool:
        """Avisa en el grupo que ese módulo no salió. True si el aviso llegó."""
        try:
            self.avisar(f"No pude armar la foto de cierre de /{modulo} del "
                        f"{fecha_larga(dia)}: {_corto(ex)}")
        except Exception as ex2:  # noqa: BLE001 - queda en el log y se reintenta
            self.log(f"Cierre: tampoco pude avisarlo en el grupo: {_corto(ex2)}")
            return False
        return True
