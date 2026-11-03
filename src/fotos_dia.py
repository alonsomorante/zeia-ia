"""Fotos del día: cómo va la cobertura de HOY, a horas fijas (mañana, tarde y noche).

A cada hora de FOTOS_DIA (.env; "09:00,14:00,20:00" por defecto, hora Lima) el
bot de Telegram manda al grupo, SIN sonido, las fotos de /ambiental, /energia
y /agua de hoy acumulado (de 00:00 a esa hora): las mismas de los comandos
(src/foto_salas.py), solo que llegan solas. Un módulo sin problemas manda solo
el texto "✅ Todo bien". La foto del día ANTERIOR completo es otra cosa: la
manda src/foto_cierre.py a las 00:10.

- Si el bot estaba apagado a esa hora, la manda al volver solo si pasó menos
  de VENTANA_MIN; si no, se salta (una foto "de las 09:00" a las 13:00 confunde).
- Lo enviado se anota en data/fotos_dia.json: un reinicio no repite nada.
- Si un módulo falla (base o Telegram caídos) se reintenta cada REINTENTO_MIN
  minutos mientras dure la ventana; después se deja (queda en el log).
- FOTOS_DIA=apagado las desactiva. No depende de AVISOS_MODO.

Lo revisa el bucle principal del bot (scripts/telegram_bot.py), igual que la
foto de cierre: sin hilos ni otro proceso.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import config
from .foto_cierre import APAGADO, LIMA, MODULOS, hora_de, texto_hora

HORAS_DEFECTO = "09:00,14:00,20:00"
VENTANA_MIN = 60
REINTENTO_MIN = 5
RUTA = config.ROOT / "data" / "fotos_dia.json"


def horas_de(texto: Optional[str]) -> list:
    """'09:00,14:00,20:00' → [540, 840, 1200]; 'apagado' → []. Ignora lo que no
    se entiende; si no queda ninguna hora válida, usa las de por defecto."""
    s = str(texto if texto is not None else HORAS_DEFECTO).strip().lower()
    if s in APAGADO:
        return []
    horas = {h for h in (hora_de(p, defecto=-1) for p in s.split(",")) if h is not None and h >= 0}
    if not horas and s != HORAS_DEFECTO:
        return horas_de(HORAS_DEFECTO)
    return sorted(horas)


def _corto(ex: Exception) -> str:
    txt = (str(ex).strip().splitlines() or [type(ex).__name__])[0]
    return txt[:200]


def _clave(hora_min: int, modulo: str) -> str:
    return f"{texto_hora(hora_min)}/{modulo}"


class FotosDia:
    """Manda las fotos de hoy a cada hora de FOTOS_DIA (lo llama el bucle del bot).

    enviar(modulo, hora_min): arma y manda la foto de hoy de un módulo; lanza si falla.
    """

    def __init__(self, enviar: Callable[[str, int], None], horas: Optional[str] = None,
                 ruta: Optional[Path] = None, ahora: Optional[Callable[[], datetime]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.horas = horas_de(config.FOTOS_DIA if horas is None else horas)
        self.enviar = enviar
        self.ruta = Path(ruta) if ruta is not None else RUTA
        self.ahora = ahora or (lambda: datetime.now(timezone.utc))
        self.log = log or (lambda m: print(m, flush=True))
        self._hechos: dict = {}        # día → claves ya enviadas (por si no se puede anotar)
        self._reintento: dict = {}     # (día, clave) → próximo intento

    def descripcion(self) -> str:
        if not self.horas:
            return "Fotos del día apagadas (FOTOS_DIA=apagado)"
        return (f"Fotos del día: a las {', '.join(texto_hora(h) for h in self.horas)} manda "
                f"cómo va hoy, sin sonido (registro en {self.ruta})")

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

    def _hechas(self, dia: date) -> set:
        guardado = self._leer()
        hechas = set(self._hechos.get(dia, ()))
        if str(guardado.get("dia") or "") == dia.isoformat():
            hechas |= set(guardado.get("hechos") or [])
        return hechas

    def pendientes(self, t: Optional[datetime] = None) -> tuple:
        """(día, hora que toca, módulos que faltan); (None, None, []) si no toca nada.

        Toca la última hora ya pasada de hoy, si pasaron menos de VENTANA_MIN."""
        tl = (t or self.ahora()).astimezone(LIMA)
        dia, minuto = tl.date(), tl.hour * 60 + tl.minute
        vigentes = [h for h in self.horas if h <= minuto < h + VENTANA_MIN]
        if not vigentes:
            return None, None, []
        hora = vigentes[-1]
        hechas = self._hechas(dia)
        faltan = [m for m in MODULOS if _clave(hora, m) not in hechas]
        return (dia, hora, faltan) if faltan else (None, None, [])

    def revisar(self) -> list:
        """Manda lo que falte de la hora que toca; devuelve los módulos enviados. No lanza."""
        try:
            return self._revisar()
        except Exception as ex:  # noqa: BLE001 - el bot no debe caerse por estas fotos
            self.log(f"Fotos del día: no pude revisar: {type(ex).__name__}: {_corto(ex)}")
            return []

    def _revisar(self) -> list:
        t = self.ahora()
        dia, hora, faltan = self.pendientes(t)
        if dia is None:
            return []
        self._hechos = {dia: self._hechos.get(dia, set())}
        self._reintento = {k: v for k, v in self._reintento.items() if k[0] == dia}
        enviados = []
        for modulo in faltan:
            clave = _clave(hora, modulo)
            proximo = self._reintento.get((dia, clave))
            if proximo is not None and t < proximo:
                continue
            try:
                self.enviar(modulo, hora)
            except Exception as ex:  # noqa: BLE001 - se reintenta mientras dure la ventana
                self._reintento[(dia, clave)] = t + timedelta(minutes=REINTENTO_MIN)
                self.log(f"Foto de las {texto_hora(hora)}: /{modulo} falló ({_corto(ex)}); "
                         f"reintento en {REINTENTO_MIN} min")
                continue
            self._reintento.pop((dia, clave), None)
            enviados.append(modulo)
            self._anotar(dia, clave)
        return enviados

    def _anotar(self, dia: date, clave: str) -> None:
        hechos = self._hechos.setdefault(dia, set())
        hechos.add(clave)
        try:
            self._guardar(dia, sorted(self._hechas(dia) | hechos))
        except OSError as ex:
            self.log(f"Fotos del día: no pude anotar lo enviado en {self.ruta}: {_corto(ex)}")
