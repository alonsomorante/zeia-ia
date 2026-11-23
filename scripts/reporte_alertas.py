"""Reporte manual de alertas y notificaciones por correo (energía, agua y ambiental).

Genera un PDF con las alertas registradas en un rango de fechas y, cuando la
plataforma lo permite, la información de notificación (marcada como reportada,
correo activo y destinatarios configurados). No envía correos ni modifica nada:
solo consulta las bases con sesión read-only (src.db) y arma un HTML que se
imprime con Chrome/Edge headless.

Fuentes (verificadas en producción):
- Energía: alerts_alert + alerts_alertthreshold (email_sending/alert_recipients).
- Agua:    alerts_wateralert + su umbral (misma tabla de umbrales).
- Reportes programados de energía: alerts_currentstatusreportschedule (estado
  actual: 1 correo por punto y horario), alerts_dailycoveragereportschedule
  (cobertura diaria) y alerts_monthlyconsumptionreportschedule (consumo
  mensual). Guardan la CONFIGURACIÓN, no un log de envíos.
- Ambiental: alerts_limitalert ("Limit Alerts"). La base ambiental no guarda un
  histórico de correos: solo el estado actual por equipo
  (equipments_device.last_missing_alert_sent). Por eso los correos de "sin
  datos" se INFIEREN de los huecos de lecturas (>= timeout del equipo) y se
  marcan como tales en el PDF.

Cada correo lleva su evidencia (ver el anexo B, Correos día por día):
- Registrado: la base guarda la marca y la hora (alertas, resumen diario).
- Programado: configuración activa y horario conocido, sin log de envío.
- Inferido: deducido de otro dato (huecos de lecturas).

Qué significa cada cifra (también se explica en el anexo A2):
- "Alerta registrada": fila que guardó la plataforma.
- "Episodio": alertas repetidas del mismo punto/tipo separadas por <= corte
  minutos (por defecto 15). Evita listar 2,000 voltajes seguidos.
- "Notif.": la plataforma marcó la alerta como reportada (`reported_at`). NO es
  un log de correos: un umbral puede tener el correo desactivado y la alerta
  igual figura como reportada.
- "Correo": el umbral tiene correo activo y destinatarios configurados HOY.
  Los destinatarios no están versionados: si cambiaron en el pasado, el PDF
  muestra la configuración actual.
- "Resumen": la alerta quedó incluida en un correo resumen de la plataforma
  (`email_summary_reported_at`, históricamente a las 17:00 hora Lima).

Uso:
    python scripts/reporte_alertas.py
    python scripts/reporte_alertas.py --desde 2026-11-02 --hasta 2026-11-08
    python scripts/reporte_alertas.py --desde 2026-10-01 --hasta 2026-10-31 \
        --empresa Sanna --detalle
"""
from __future__ import annotations

import argparse
import html
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# En Windows la consola usa cp1252; forzar UTF-8 para imprimir "→", "·", etc.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402

from src import tunnel  # noqa: E402
from src.db import get_engine, validate_sql  # noqa: E402
from src.monitor import EXCLUIR as EMPRESAS_PRUEBA  # noqa: E402
from src.reporte_pdf import buscar_chrome, imprimir_pdf  # noqa: E402

LIMA = timezone(timedelta(hours=-5))  # Perú no tiene horario de verano
MODULOS = ("energia", "agua", "ambiental")
NOMBRE_MODULO = {"energia": "Energía", "agua": "Agua", "ambiental": "Ambiental"}
CLASE_MODULO = {"energia": "e", "agua": "a", "ambiental": "amb"}
ORDEN_MODULO = {"energia": 0, "agua": 1, "ambiental": 2}

DIAS_CORTOS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MESES_LARGOS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

CORTE_MIN = 15            # minutos para agrupar alertas en un episodio
HORARIO_INI = 8 * 60      # horario laboral (mismo criterio que src/avisos.py)
HORARIO_FIN = 20 * 60
DIAS_MAX = 120            # protección de consultas/PDF para rangos largos

# Regla de correo de ambiental (confirmada por el negocio el 13-nov-2026):
# solo Humedad y Temperatura generan correo, y solo en Sanna. CO₂ no genera.
# La base no guarda el envío ni los destinatarios: esto es configuración
# conocida, no un log. Si cambia, actualizar aquí y en el anexo A1.
DEST_AMBIENTAL = (
    "diego.florian@sanna.pe",
    "franco.ordonez@sanna.pe",
    "julio.villavicencio@sanna.pe",
    "marco.tasayco@sanna.pe",
    "oscar.alania@sanna.pe",
)
INDICADORES_CON_CORREO = ("Humedad", "Temperatura")

TIPO_ENERGIA = {
    "voltage_fluctuation": "Fluctuación de voltaje",
    "current_monitoring": "Monitoreo de corriente",
    "power_demand": "Demanda de potencia",
    "harmonic_distortion": "Distorsión armónica",
    "energy_monitoring": "Monitoreo de energía",
    "average_consumption": "Consumo promedio",
    "water_average_consumption": "Consumo de agua promedio",
}
SUBTIPOS = {
    "undervoltage": "Subtensión",
    "overvoltage": "Sobretensión",
    "max_current_exceeded": "Corriente máxima excedida",
    "min_current_subceeded": "Corriente mínima",
    "zero_current": "Corriente cero",
    "equipment_on": "Equipo encendido",
    "current_unbalance": "Desbalance de corriente",
    "overconsumption": "Sobreconsumo",
    "average_overconsumption": "Sobreconsumo vs promedio",
    "reactive_inductive_exceeded": "Reactiva inductiva excedida",
}
UNIDAD_ENERGIA = {
    "voltage_fluctuation": "V",
    "current_monitoring": "A",
    "power_demand": "kW",
    "harmonic_distortion": "%",
    "energy_monitoring": "kWh",
    "average_consumption": "kWh",
    "water_average_consumption": "m³",
}
NIVELES = {
    "HUMIDITY_MAX": "Humedad alta",
    "HUMIDITY_MIN": "Humedad baja",
    "TEMP_MAX": "Temperatura alta",
    "TEMP_MIN": "Temperatura baja",
    "UNHEALTHY": "CO₂ no saludable",
    "DANGEROUS": "CO₂ peligroso",
    "CRITICAL": "CO₂ crítico",
    "MODERATE": "CO₂ moderado",
    "GOOD": "CO₂ bueno",
}
INDICADORES = {"CO2": "CO₂", "TEMPERATURE": "Temperatura", "HUMIDITY": "Humedad"}
UNIDADES = {"PERCENT": "%", "PPM": "ppm", "CELSIUS": "°C", "C": "°C", "MG_M3": "mg/m³"}
RANGO_SEV = {"Crítica": 4, "Alta": 3, "Moderada": 2, "Media": 2, "Baja": 1, "Alerta": 2}


# ---------- Modelo ------------------------------------------------------------

@dataclass
class Evento:
    """Una alerta registrada (energía o agua), normalizada para el reporte."""
    modulo: str
    empresa: str
    sede: str
    grupo: str
    punto: str
    tipo: str
    subtipo: str
    severidad: str
    cuando: datetime
    valor: Optional[float]
    unidad: str
    nota: str
    destinatarios: tuple = ()
    correo_activo: Optional[bool] = None
    reportada: bool = False
    resumen: bool = False
    resumen_cuando: Optional[datetime] = None
    reportada_cuando: Optional[datetime] = None


@dataclass
class Episodio:
    modulo: str
    empresa: str
    sede: str
    grupo: str
    punto: str
    tipo: str
    subtipo: str
    severidad: str
    inicio: datetime
    fin: datetime
    n: int
    valor_min: Optional[float]
    valor_max: Optional[float]
    unidad: str
    nota: str
    destinatarios: tuple = ()
    correo_activo: Optional[bool] = None
    reportadas: int = 0
    resumen: int = 0
    resumen_cuando: Optional[datetime] = None

    @property
    def minutos(self) -> float:
        return max(0.0, (self.fin - self.inicio).total_seconds() / 60)

    @property
    def notificacion(self) -> str:
        if self.reportadas >= self.n:
            return "Sí"
        return "Parcial" if self.reportadas else "—"


@dataclass
class Limite:
    """Una alerta de límite ambiental (alerts_limitalert)."""
    empresa: str
    sede: str
    sala: str
    equipo: str
    indicador: str
    nivel: str
    cuando: datetime
    valor: Optional[float]
    unidad: str
    medicion: str
    resuelto: bool


@dataclass
class GrupoLimite:
    empresa: str
    sede: str
    sala: str
    equipo: str
    indicador: str
    nivel: str
    n: int = 0
    dias: list = field(default_factory=list)
    primera: Optional[datetime] = None
    ultima: Optional[datetime] = None
    valor_min: Optional[float] = None
    valor_max: Optional[float] = None
    unidad: str = ""
    mediciones: Counter = field(default_factory=Counter)
    resueltas: int = 0
    no_resueltas: int = 0


@dataclass
class Hueco:
    """Episodio sin datos de un equipo ambiental (correo de sin datos inferido)."""
    empresa: str
    sede: str
    sala: str
    equipo: str
    inicio: datetime
    fin: datetime
    minutos: float
    abierto: bool
    venia_de_antes: bool
    sin_lecturas: bool = False
    destinatarios: tuple = ()
    correo_activo: bool = False


@dataclass
class Correo:
    """Un correo del periodo (o un grupo de correos idénticos).

    `evidencia` distingue lo que la base registra de lo que solo se deduce:
    - Registrado: la base guarda la marca y la hora del envío.
    - Programado: la configuración está activa a esa hora; no hay log de envío.
    - Inferido: se deduce de otro dato (p. ej. un hueco de lecturas).
    """
    sistema: str                    # Energía / Agua / Ambiental
    tipo: str                       # Alerta, Resumen diario, Estado actual…
    evidencia: str                  # Registrado / Programado / Inferido
    dia: date
    hora: Optional[time]
    empresa: str = ""
    sede: str = ""
    detalle: str = ""
    n: int = 1                      # correos agrupados en esta fila
    destinatarios: tuple = ()
    nota: str = ""
    frecuencia: str = ""

    @property
    def cuando(self) -> Optional[datetime]:
        if self.hora is None:
            return None
        return datetime.combine(self.dia, self.hora, tzinfo=LIMA)


@dataclass
class ConfigUmbral:
    empresa: str
    destinatarios: tuple
    correo: bool
    tipos: str
    n: int


@dataclass
class ConfigProgramado:
    empresa: str
    tipo: str
    cuando: str
    destinatarios: tuple
    activo: bool
    n: int


# ---------- Utilidades de fechas, números y texto ----------------------------

def inicio_dia(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=LIMA)


def fin_dia(d: date) -> datetime:
    return inicio_dia(d + timedelta(days=1))


def semana_pasada(hoy: date) -> tuple:
    lunes = hoy - timedelta(days=hoy.weekday() + 7)
    return lunes, lunes + timedelta(days=6)


def cortar_semanas(d1: date, d2: date) -> list:
    """Rango <= 7 días = una sección; si es más largo, corta por semanas ISO."""
    if (d2 - d1).days < 7:
        return [(d1, d2)]
    out = []
    cur = d1
    while cur <= d2:
        fin = min(cur + timedelta(days=6 - cur.weekday()), d2)
        out.append((cur, fin))
        cur = fin + timedelta(days=1)
    return out


def fmt_n(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")


def plural(n: int, singular: str, plural_: Optional[str] = None) -> str:
    """'1 episodio' / '3 episodios'."""
    if int(n) == 1:
        return f"{fmt_n(n)} {singular}"
    return f"{fmt_n(n)} {plural_ or singular + 's'}"


def fmt_dec(v: Optional[float], dec: int = 1) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    s = f"{v:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_pct(v: float) -> str:
    return fmt_dec(v, 1) + "%"


def fmt_fecha(d: date) -> str:
    return f"{DIAS_CORTOS[d.weekday()]} {d.day:02d} {MESES_ES[d.month - 1]}"


def fmt_fecha_larga(d: date) -> str:
    return f"{d.day} de {MESES_LARGOS[d.month - 1]} de {d.year}"


def fmt_fecha_corta(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}"


def fmt_hora(dt: datetime) -> str:
    return dt.astimezone(LIMA).strftime("%H:%M")


def fmt_fecha_hora(dt: datetime) -> str:
    x = dt.astimezone(LIMA)
    return f"{fmt_fecha_corta(x.date())} {x.strftime('%H:%M')}"


def fmt_rango_horas(a: datetime, b: datetime) -> str:
    a, b = a.astimezone(LIMA), b.astimezone(LIMA)
    if a.date() == b.date():
        if (a.hour, a.minute) == (b.hour, b.minute):
            return a.strftime("%H:%M")
        return f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')}"
    return f"{a.strftime('%H:%M')} → {fmt_fecha_corta(b.date())} {b.strftime('%H:%M')}"


def fmt_duracion(minutos: float) -> str:
    m = int(round(minutos))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h {m:02d} min" if m else f"{h} h"
    d, h = divmod(h, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def fmt_valor(vmin: Optional[float], vmax: Optional[float], unidad: str = "") -> str:
    if vmin is None and vmax is None:
        return "—"
    dec = 2 if unidad in ("kWh", "m³") else 1
    u = f" {unidad}" if unidad else ""
    if vmin is None:
        return fmt_dec(vmax, dec) + u
    if vmax is None or abs(vmax - vmin) < 10 ** (-dec):
        return fmt_dec(vmin, dec) + u
    return f"{fmt_dec(vmin, dec)}–{fmt_dec(vmax, dec)}{u}"


def rango_largo(d1: date, d2: date) -> str:
    if d1 == d2:
        return fmt_fecha_larga(d1)
    if d1.year != d2.year:
        return f"{fmt_fecha_larga(d1)} al {fmt_fecha_larga(d2)}"
    if d1.month == d2.month:
        return f"{d1.day} al {d2.day} de {MESES_LARGOS[d2.month - 1]} de {d2.year}"
    return (f"{d1.day} de {MESES_LARGOS[d1.month - 1]} al {d2.day} de "
            f"{MESES_LARGOS[d2.month - 1]} de {d2.year}")


def etiqueta_tipo(codigo: Optional[str]) -> str:
    if not codigo:
        return "Alerta"
    return TIPO_ENERGIA.get(codigo, str(codigo).replace("_", " ").capitalize())


def etiqueta_subtipo(codigo: Optional[str]) -> str:
    if not codigo:
        return ""
    return SUBTIPOS.get(codigo, str(codigo).replace("_", " ").capitalize())


def etiqueta_nivel(nivel: Optional[str]) -> str:
    if not nivel:
        return "Alerta"
    return NIVELES.get(nivel, str(nivel).replace("_", " ").capitalize())


def etiqueta_indicador(ind: Optional[str]) -> str:
    if not ind:
        return "—"
    return INDICADORES.get(ind, str(ind).capitalize())


def severidad_alerta(status: Optional[str]) -> str:
    s = (status or "").strip().lower()
    if s == "critical":
        return "Crítica"
    if s == "moderate":
        return "Moderada"
    if s in ("dangerous", "danger"):
        return "Crítica"
    if s == "unhealthy":
        return "Alta"
    return "Alerta"


def severidad_ambiental(level: Optional[str]) -> str:
    lv = (level or "").upper()
    if lv in ("CRITICAL", "DANGEROUS", "TEMP_MAX"):
        return "Crítica"
    if lv in ("UNHEALTHY", "HUMIDITY_MAX", "HUMIDITY_MIN", "TEMP_MIN"):
        return "Alta"
    if lv == "MODERATE":
        return "Media"
    if lv == "GOOD":
        return "Baja"
    return "Media"


def peor_severidad(a: str, b: str) -> str:
    return a if RANGO_SEV.get(a, 0) >= RANGO_SEV.get(b, 0) else b


def parse_destinatarios(texto: Optional[str]) -> tuple:
    """'a@x, b@y; a@x' -> ('a@x', 'b@y'), sin vacíos ni repetidos."""
    if not texto:
        return ()
    out = []
    vistos = set()
    for parte in re.split(r"[,;\s]+", str(texto)):
        correo = parte.strip().strip("<>").lower()
        if not correo or "@" not in correo or correo in vistos:
            continue
        vistos.add(correo)
        out.append(correo)
    return tuple(out)


def unir(items: Iterable[str], tope: int = 3) -> str:
    vals = [x for x in items if x]
    if not vals:
        return ""
    if len(vals) <= tope:
        return ", ".join(vals)
    return ", ".join(vals[:tope]) + f" (+{len(vals) - tope})"


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "empresa"


def _es_prueba(empresa: Optional[str]) -> bool:
    return (empresa or "").strip().lower() in EMPRESAS_PRUEBA


def _num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _recorta(texto: Optional[str], n: int = 170) -> str:
    t = re.sub(r"\s+", " ", (texto or "")).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


esc = html.escape


# ---------- Agrupación --------------------------------------------------------

def agrupar_episodios(eventos: Sequence[Evento], corte_min: int = CORTE_MIN) -> list:
    """Une alertas del mismo punto/tipo separadas por <= corte_min minutos.

    La severidad resultante es la peor del episodio; los destinatarios y
    banderas de correo se unen (si alguna alerta tenía correo activo, el
    episodio lo muestra). La nota es la del último evento.
    """
    if not eventos:
        return []
    orden = sorted(eventos, key=lambda e: (
        ORDEN_MODULO.get(e.modulo, 9), e.empresa, e.sede, e.grupo, e.punto,
        e.tipo, e.subtipo, e.cuando,
    ))
    out = []
    actual = None
    fin = None
    for ev in orden:
        clave = (ev.modulo, ev.empresa, ev.sede, ev.grupo, ev.punto, ev.tipo, ev.subtipo)
        if actual is not None and actual["clave"] == clave and \
                (ev.cuando - fin) <= timedelta(minutes=corte_min):
            actual["fin"] = ev.cuando
            actual["n"] += 1
            actual["severidad"] = peor_severidad(actual["severidad"], ev.severidad)
            actual["valor_min"] = min(x for x in (actual["valor_min"], ev.valor) if x is not None) \
                if (actual["valor_min"] is not None or ev.valor is not None) else None
            actual["valor_max"] = max(x for x in (actual["valor_max"], ev.valor) if x is not None) \
                if (actual["valor_max"] is not None or ev.valor is not None) else None
            if not actual["unidad"]:
                actual["unidad"] = ev.unidad
            if ev.nota:
                actual["nota"] = ev.nota
            actual["destinatarios"] = _union(actual["destinatarios"], ev.destinatarios)
            actual["correo_activo"] = _unir_bandera(actual["correo_activo"], ev.correo_activo)
            actual["reportadas"] += int(bool(ev.reportada))
            actual["resumen"] += int(bool(ev.resumen))
            if ev.resumen_cuando:
                actual["resumen_cuando"] = ev.resumen_cuando
            fin = ev.cuando
            continue
        if actual is not None:
            out.append(_episodio(actual))
        actual = {
            "clave": clave,
            "modulo": ev.modulo, "empresa": ev.empresa, "sede": ev.sede,
            "grupo": ev.grupo, "punto": ev.punto, "tipo": ev.tipo,
            "subtipo": ev.subtipo, "severidad": ev.severidad,
            "inicio": ev.cuando, "fin": ev.cuando, "n": 1,
            "valor_min": ev.valor, "valor_max": ev.valor, "unidad": ev.unidad,
            "nota": ev.nota, "destinatarios": ev.destinatarios,
            "correo_activo": ev.correo_activo,
            "reportadas": int(bool(ev.reportada)),
            "resumen": int(bool(ev.resumen)),
            "resumen_cuando": ev.resumen_cuando,
        }
        fin = ev.cuando
    if actual is not None:
        out.append(_episodio(actual))
    return out


def _union(a: tuple, b: tuple) -> tuple:
    out = list(a or ())
    for x in b or ():
        if x not in out:
            out.append(x)
    return tuple(out)


def _unir_bandera(a: Optional[bool], b: Optional[bool]) -> Optional[bool]:
    if a is None:
        return b
    if b is None:
        return a
    return bool(a or b)


def _episodio(d: dict) -> Episodio:
    return Episodio(
        modulo=d["modulo"], empresa=d["empresa"], sede=d["sede"], grupo=d["grupo"],
        punto=d["punto"], tipo=d["tipo"], subtipo=d["subtipo"],
        severidad=d["severidad"], inicio=d["inicio"], fin=d["fin"], n=d["n"],
        valor_min=d["valor_min"], valor_max=d["valor_max"], unidad=d["unidad"],
        nota=d["nota"], destinatarios=d["destinatarios"],
        correo_activo=d["correo_activo"], reportadas=d["reportadas"],
        resumen=d["resumen"], resumen_cuando=d["resumen_cuando"],
    )


def agrupar_limites(limites: Sequence[Limite], dias: Sequence[date]) -> list:
    """Agrupa Limit Alerts por empresa/sede/sala/equipo/indicador/nivel."""
    grupos = {}
    for l in limites:
        clave = (l.empresa, l.sede, l.sala, l.equipo, l.indicador, l.nivel)
        g = grupos.get(clave)
        if g is None:
            g = GrupoLimite(empresa=l.empresa, sede=l.sede, sala=l.sala,
                            equipo=l.equipo, indicador=l.indicador, nivel=l.nivel,
                            dias=[0] * len(dias))
            grupos[clave] = g
        g.n += 1
        idx = (l.cuando.date() - dias[0]).days
        if 0 <= idx < len(g.dias):
            g.dias[idx] += 1
        if g.primera is None or l.cuando < g.primera:
            g.primera = l.cuando
        if g.ultima is None or l.cuando > g.ultima:
            g.ultima = l.cuando
        if l.valor is not None:
            g.valor_min = l.valor if g.valor_min is None else min(g.valor_min, l.valor)
            g.valor_max = l.valor if g.valor_max is None else max(g.valor_max, l.valor)
        if not g.unidad:
            g.unidad = l.unidad
        g.mediciones[l.medicion] += 1
        if l.resuelto:
            g.resueltas += 1
        else:
            g.no_resueltas += 1
    return sorted(grupos.values(),
                  key=lambda g: (g.empresa, g.sede, g.sala, -g.n,
                                 -RANGO_SEV.get(severidad_ambiental(g.nivel), 0),
                                 g.indicador))


# ---------- Consultas (solo lectura) -----------------------------------------

def consultar(base: str, sql: str, **params):
    seguro = validate_sql(sql)
    with get_engine(base).connect() as conn:
        return conn.execute(text(seguro), params).fetchall()


def _lima(dt: datetime) -> datetime:
    if dt is None:
        return dt
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).astimezone(LIMA)
    return dt.astimezone(LIMA)


SQL_ENERGIA = """
    SELECT a.id, a."timestamp" AS cuando, a.reported, a."reported_at",
           a.email_summary_reported,
           a."email_summary_reported_at" AS resumen_cuando,
           a.alert_status, a.value, a.notes,
           a.fluctuation_subtype, a.current_subtype, a.power_subtype,
           a.energy_subtype, a.unbalanced_subtype,
           t.alert_type, t.email_sending,
           COALESCE(t.email_recipients, '') AS recips,
           COALESCE(e.name, '(sin empresa)') AS empresa,
           COALESCE(eh.name, '') AS sede,
           COALESCE(ep.name, '') AS tablero,
           COALESCE(mp.name, '(sin punto)') AS punto
    FROM alerts_alert a
    JOIN alerts_alertthreshold t ON t.id = a.alert_threshold_id
    LEFT JOIN enterprises_enterprise e ON e.id = t.enterprise_id
    LEFT JOIN enterprises_energyheadquarter eh ON eh.id = t.energy_headquarter_id
    LEFT JOIN enterprises_electricalpanel ep ON ep.id = t.electrical_panel_id
    LEFT JOIN enterprises_measurementpoint mp ON mp.id = t.measurement_point_id
    WHERE a."timestamp" >= :ini AND a."timestamp" < :fin
    ORDER BY a."timestamp"
"""

SQL_AGUA = """
    SELECT w.id, w."timestamp" AS cuando, w.value_m3, w.average_m3,
           w.alert_status, w.notes, w.reported, w."reported_at",
           t.email_sending, COALESCE(t.email_recipients, '') AS recips,
           COALESCE(e_t.name, e_w.name, '(sin empresa)') AS empresa,
           COALESCE(eh_t.name, eh_w.name, '') AS sede,
           COALESCE(wp.name, '') AS tuberia,
           COALESCE(mw.name, '(sin medidor)') AS medidor
    FROM alerts_wateralert w
    JOIN alerts_alertthreshold t ON t.id = w.alert_threshold_id
    LEFT JOIN enterprises_enterprise e_t ON e_t.id = t.enterprise_id
    LEFT JOIN enterprises_energyheadquarter eh_t ON eh_t.id = t.energy_headquarter_id
    LEFT JOIN enterprises_measurementpointwater mw ON mw.id = t.measurement_point_water_id
    LEFT JOIN enterprises_waterpipe wp ON wp.id = mw.water_pipe_id
    LEFT JOIN enterprises_energyheadquarter eh_w ON eh_w.id = wp.energy_headquarter_id
    LEFT JOIN enterprises_enterprise e_w ON e_w.id = eh_w.enterprise_id
    WHERE w."timestamp" >= :ini AND w."timestamp" < :fin
    ORDER BY w."timestamp"
"""

SQL_AMBIENTAL = """
    SELECT a.id, a.created_at AS cuando, a.device_id, a.indicator, a.level,
           a.type_value, a.value, a.unit, a.resolved,
           COALESCE(r.name, '(sin sala)') AS sala,
           COALESCE(h.name, '') AS sede,
           COALESCE(e.name, '(sin empresa)') AS empresa,
           COALESCE(d.type_sensor, 'equipo') AS tipo_sensor
    FROM alerts_limitalert a
    LEFT JOIN enterprise_room r ON r.id = a.room_id
    LEFT JOIN enterprise_headquarters h ON h.id = r.headquarter_id
    LEFT JOIN enterprise_enterprise e ON e.id = h.enterprise_id
    LEFT JOIN equipments_device d ON d.id = a.device_id
    WHERE a.created_at >= :ini AND a.created_at < :fin
    ORDER BY a.created_at
"""

SQL_AMBIENTAL_DEVICES = """
    SELECT d.id, COALESCE(d.type_sensor, 'equipo') AS tipo_sensor,
           d.no_data_alert, d.timeout_minutes, d.reminder_minutes,
           COALESCE(d.alert_recipient_emails, '') AS recips,
           d.no_data_status, d.down_started_at, d.last_missing_alert_sent,
           d.is_activated,
           COALESCE(r.name, '(sin sala)') AS sala,
           COALESCE(h.name, '') AS sede,
           COALESCE(e.name, '(sin empresa)') AS empresa,
           r.is_activated AS sala_activa
    FROM equipments_device d
    LEFT JOIN enterprise_room r ON r.id = d.room_id
    LEFT JOIN enterprise_headquarters h ON h.id = r.headquarter_id
    LEFT JOIN enterprise_enterprise e ON e.id = h.enterprise_id
    WHERE d.no_data_alert
    ORDER BY empresa, sede, sala, d.id
"""

SQL_UMBRALES = """
    SELECT t.id, t.name, t.alert_type, t.is_active, t.email_sending,
           COALESCE(t.email_recipients, '') AS recips,
           COALESCE(e.name, '(sin empresa)') AS empresa
    FROM alerts_alertthreshold t
    LEFT JOIN enterprises_enterprise e ON e.id = t.enterprise_id
    ORDER BY empresa, t.name
"""

SQL_PROGRAMADOS_STATUS = """
    SELECT s.id, s.report_time, COALESCE(s.email_recipients, '') AS recips,
           s.is_active, COALESCE(s.description, '') AS descripcion,
           COALESCE(e.name, '(sin empresa)') AS empresa
    FROM alerts_currentstatusreportschedule s
    JOIN alerts_alertthreshold t ON t.id = s.alert_threshold_id
    LEFT JOIN enterprises_enterprise e ON e.id = t.enterprise_id
    ORDER BY empresa, s.report_time
"""

SQL_PROGRAMADOS_DIARIO = """
    SELECT s.id, COALESCE(s.email_recipients, '') AS recips, s.is_active,
           COALESCE(s.description, '') AS descripcion,
           COALESCE(e.name, '(sin empresa)') AS empresa
    FROM alerts_dailycoveragereportschedule s
    LEFT JOIN enterprises_enterprise e ON e.id = s.enterprise_id
    ORDER BY empresa
"""

SQL_PROGRAMADOS_MENSUAL = """
    SELECT s.id, COALESCE(s.email_recipients, '') AS recips, s.is_active,
           COALESCE(s.description, '') AS descripcion,
           COALESCE(e.name, '(sin empresa)') AS empresa
    FROM alerts_monthlyconsumptionreportschedule s
    LEFT JOIN enterprises_enterprise e ON e.id = s.enterprise_id
    ORDER BY empresa
"""

SQL_RESUMENES = """
    SELECT date_trunc('minute', email_summary_reported_at) AS momento, count(*) AS n
    FROM alerts_alert
    WHERE email_summary_reported
      AND email_summary_reported_at >= :ini AND email_summary_reported_at < :fin
    GROUP BY 1 ORDER BY 1
"""

SQL_ULTIMO_RESUMEN = """
    SELECT max(email_summary_reported_at) AS ultimo
    FROM alerts_alert
    WHERE email_summary_reported
"""

SQL_ESTADO_ACTUAL = """
    SELECT s.id, s.report_time, s.is_active, s.should_be_on,
           COALESCE(s.description, '') AS descripcion,
           COALESCE(s.email_recipients, '') AS recips,
           COALESCE(e.name, '(sin empresa)') AS empresa,
           COALESCE(eh.name, '') AS sede,
           COALESCE(mp.name, '(sin punto)') AS punto
    FROM alerts_currentstatusreportschedule s
    JOIN alerts_alertthreshold t ON t.id = s.alert_threshold_id
    LEFT JOIN enterprises_enterprise e ON e.id = t.enterprise_id
    LEFT JOIN enterprises_energyheadquarter eh ON eh.id = t.energy_headquarter_id
    LEFT JOIN enterprises_measurementpoint mp ON mp.id = t.measurement_point_id
    ORDER BY s.report_time, empresa, punto
"""

SQL_COBERTURA = """
    SELECT COALESCE(e.name, '(sin empresa)') AS empresa,
           (r.created_at AT TIME ZONE 'America/Lima')::date AS dia,
           count(*) AS lecturas,
           count(DISTINCT mp.id) AS puntos
    FROM readings_reading r
    JOIN enterprises_measurementpoint mp ON mp.id = r.measurement_point_id
    JOIN devices_device d ON d.id = mp.device_id
    JOIN enterprises_electricalpanel ep ON ep.id = d.electrical_panel_id
    JOIN enterprises_energyheadquarter eh ON eh.id = ep.energy_headquarter_id
    JOIN enterprises_enterprise e ON e.id = eh.enterprise_id
    WHERE r.created_at >= :ini AND r.created_at < :fin
    GROUP BY 1, 2
"""

SQL_NODATA_ENERGIA = """
    SELECT n.id, n.timeout_minutes, n.reminder_minutes,
           COALESCE(n.email_recipients, '') AS recips, n.is_active, n.status,
           n.down_since, n.alert_sent_at, n.reminder_sent_at,
           COALESCE(e.name, '(sin empresa)') AS empresa,
           COALESCE(eh.name, '') AS sede,
           COALESCE(mp.name, '(sin punto)') AS punto
    FROM alerts_nodatamonitor n
    JOIN enterprises_measurementpoint mp ON mp.id = n.measurement_point_id
    JOIN devices_device d ON d.id = mp.device_id
    JOIN enterprises_electricalpanel ep ON ep.id = d.electrical_panel_id
    JOIN enterprises_energyheadquarter eh ON eh.id = ep.energy_headquarter_id
    JOIN enterprises_enterprise e ON e.id = eh.enterprise_id
    ORDER BY empresa, sede, punto
"""


def _subtipo_energia(m) -> str:
    for col in ("fluctuation_subtype", "current_subtype", "power_subtype",
                "energy_subtype", "unbalanced_subtype"):
        if m[col]:
            return etiqueta_subtipo(m[col])
    return ""


def cargar_energia(ini: datetime, fin: datetime) -> list:
    out = []
    for r in consultar("energia", SQL_ENERGIA, ini=ini, fin=fin):
        m = r._mapping
        empresa = m["empresa"]
        if _es_prueba(empresa):
            continue
        recips = parse_destinatarios(m["recips"])
        out.append(Evento(
            modulo="energia", empresa=empresa, sede=m["sede"] or "—",
            grupo=m["tablero"] or "—", punto=m["punto"],
            tipo=etiqueta_tipo(m["alert_type"]),
            subtipo=_subtipo_energia(m),
            severidad=severidad_alerta(m["alert_status"]),
            cuando=_lima(m["cuando"]), valor=_num(m["value"]),
            unidad=UNIDAD_ENERGIA.get(m["alert_type"], ""),
            nota=_recorta(m["notes"]),
            destinatarios=recips,
            correo_activo=bool(m["email_sending"]) and bool(recips),
            reportada=bool(m["reported"]),
            resumen=bool(m["email_summary_reported"]),
            resumen_cuando=_lima(m["resumen_cuando"]) if m["resumen_cuando"] else None,
            reportada_cuando=_lima(m["reported_at"]) if m["reported_at"] else None,
        ))
    return out


def cargar_agua(ini: datetime, fin: datetime) -> list:
    out = []
    for r in consultar("energia", SQL_AGUA, ini=ini, fin=fin):
        m = r._mapping
        empresa = m["empresa"]
        if _es_prueba(empresa):
            continue
        recips = parse_destinatarios(m["recips"])
        nota = m["notes"] or ""
        if not nota and m["average_m3"] is not None and m["value_m3"] is not None:
            nota = (f"Consumo de {fmt_dec(_num(m['value_m3']), 3)} m³ vs promedio "
                    f"{fmt_dec(_num(m['average_m3']), 3)} m³")
        out.append(Evento(
            modulo="agua", empresa=empresa, sede=m["sede"] or "—",
            grupo=m["tuberia"] or "—", punto=m["medidor"],
            tipo=etiqueta_tipo("water_average_consumption"), subtipo="",
            severidad=severidad_alerta(m["alert_status"]),
            cuando=_lima(m["cuando"]), valor=_num(m["value_m3"]), unidad="m³",
            nota=_recorta(nota), destinatarios=recips,
            correo_activo=bool(m["email_sending"]) and bool(recips),
            reportada=bool(m["reported"]), resumen=False,
            reportada_cuando=_lima(m["reported_at"]) if m["reported_at"] else None,
        ))
    return out


def cargar_ambiental(ini: datetime, fin: datetime) -> list:
    out = []
    for r in consultar("ambiental", SQL_AMBIENTAL, ini=ini, fin=fin):
        m = r._mapping
        empresa = m["empresa"]
        if _es_prueba(empresa):
            continue
        out.append(Limite(
            empresa=empresa, sede=m["sede"] or "—", sala=m["sala"],
            equipo=f"{m['tipo_sensor']} #{m['device_id']}",
            indicador=etiqueta_indicador(m["indicator"]),
            nivel=m["level"] or "",
            cuando=_lima(m["cuando"]), valor=_num(m["value"]),
            unidad=UNIDADES.get((m["unit"] or "").upper(), m["unit"] or ""),
            medicion=m["type_value"] or "Instantáneo",
            resuelto=bool(m["resolved"]),
        ))
    return out


def cargar_huecos(d1: date, d2: date) -> list:
    """Episodios sin datos de equipos ambientales con aviso de correo activo.

    La base no guarda el histórico de esos correos: se reconstruyen desde los
    huecos de lecturas (>= timeout_minutes del equipo) y se marcan como
    inferidos en el PDF.
    """
    ini, fin = inicio_dia(d1), fin_dia(d2)
    ahora = datetime.now(timezone.utc)
    tope = min(fin, ahora)
    out = []
    for r in consultar("ambiental", SQL_AMBIENTAL_DEVICES):
        m = r._mapping
        empresa = m["empresa"]
        if _es_prueba(empresa) or not m["is_activated"] or m["sala_activa"] is False:
            continue
        ids = [x[0] for x in consultar(
            "ambiental",
            "SELECT id FROM equipments_indicatordevice WHERE device_id = :dev AND is_activated",
            dev=m["id"],
        )]
        if not ids:
            continue
        filas = consultar(
            "ambiental",
            """SELECT DISTINCT date_trunc('minute', created_at) AS ts
               FROM readings_reading
               WHERE indicator_device_id = ANY(:ids)
                 AND created_at >= :ini AND created_at < :fin
               ORDER BY 1""",
            ids=ids, ini=ini, fin=fin,
        )
        antes = consultar(
            "ambiental",
            """SELECT max(created_at) FROM readings_reading
               WHERE indicator_device_id = ANY(:ids) AND created_at < :ini""",
            ids=ids, ini=ini,
        )
        prev = antes[0][0] if antes else None
        timeout = int(m["timeout_minutes"] or 60)
        recips = parse_destinatarios(m["recips"])
        equipo = f"{m['tipo_sensor']} #{m['id']}"

        def add(inicio, final, abierto=False, sin_lecturas=False):
            inicio = max(inicio, ini)
            final = min(final, tope)
            mins = (final - inicio).total_seconds() / 60
            if mins < timeout:
                return
            out.append(Hueco(
                empresa=empresa, sede=m["sede"] or "—", sala=m["sala"],
                equipo=equipo, inicio=inicio.astimezone(LIMA),
                fin=final.astimezone(LIMA), minutos=mins, abierto=abierto,
                venia_de_antes=(prev is not None and prev < ini) or sin_lecturas,
                sin_lecturas=sin_lecturas,
                destinatarios=recips, correo_activo=bool(recips),
            ))

        if prev is None and not filas:
            # Nunca reportó (ni antes ni dentro del rango): se marca aparte,
            # porque no hay forma de saber cuándo empezó la caída.
            add(ini, tope, abierto=True, sin_lecturas=True)
            continue
        for x in filas:
            ts = _lima(x[0])
            if prev is not None and (ts - prev) > timedelta(minutes=timeout):
                add(prev, ts)
            prev = ts
        if prev is not None and (tope - prev) > timedelta(minutes=timeout):
            add(prev, tope, abierto=True)
    return sorted(out, key=lambda h: (h.empresa, h.sede, h.sala, h.inicio))


# ---------- Correos del periodo -------------------------------------------------

def cargar_cobertura(d1: date, d2: date) -> dict:
    """Cobertura de datos por empresa y día (energía: 1 lectura/min/punto).

    Devuelve {(empresa, dia): {"lecturas", "puntos", "pct"}}. Se toma el máximo
    de puntos vistos en el periodo como denominador, para que un punto caído
    todo el día no inflé el porcentaje.
    """
    filas = consultar("energia", SQL_COBERTURA, ini=inicio_dia(d1), fin=fin_dia(d2))
    por_empresa = defaultdict(list)
    for r in filas:
        m = r._mapping
        por_empresa[m["empresa"]].append(
            (m["dia"], int(m["lecturas"]), int(m["puntos"])))
    out = {}
    for emp, items in por_empresa.items():
        max_puntos = max((p for _, _, p in items), default=0)
        for dia, lecturas, _ in items:
            esperadas = max_puntos * 1440
            pct = min(100.0, 100.0 * lecturas / esperadas) if esperadas else 0.0
            out[(emp, dia)] = {"lecturas": lecturas, "puntos": max_puntos,
                               "pct": pct}
    return out


def _puntos_texto(puntos: Sequence[str]) -> str:
    vistos = []
    for p in puntos:
        if p and p not in vistos:
            vistos.append(p)
    return ", ".join(vistos)


def _hora_limpia(t) -> Optional[time]:
    return t.replace(microsecond=0) if t is not None else None


def cargar_correos(d1: date, d2: date, eventos: Sequence[Evento],
                   limites: Sequence[Limite], huecos: Sequence[Hueco],
                   config: dict, cobertura: Optional[dict] = None) -> list:
    """Todo correo que el sistema pudo enviar en el periodo, con su evidencia.

    - Registrado: la base guarda la marca y la hora (alertas y resumen diario).
    - Programado: la configuración está activa y se conoce el horario, pero la
      base no guarda un log de envíos (estado actual, cobertura, consumo) o la
      regla de correo es conocida (ambiental: humedad y temperatura en Sanna).
    - Inferido: se deduce de los huecos de lecturas (sin datos ambiental).

    Los avisos de "sin datos" de energía no se cuentan: son condicionales y la
    base no registró ningún envío (ver nota en la portada).
    """
    dias = [d1 + timedelta(days=i) for i in range((d2 - d1).days + 1)]
    out = []

    # 1) Alertas de energía y agua (registradas).
    for e in eventos:
        if not e.reportada:
            continue
        # Se fecha en el día del envío (reported_at); si la alerta fue otro
        # día, se anota. Así el anexo refleja cuándo salió el correo.
        cuando = e.reportada_cuando or e.cuando
        nota = ""
        if e.reportada_cuando:
            if e.reportada_cuando.date() != e.cuando.date():
                nota = f"alerta de {fmt_fecha_hora(e.cuando)}"
            elif abs((e.reportada_cuando - e.cuando).total_seconds()) >= 60:
                nota = f"alerta de {fmt_hora(e.cuando)}"
        out.append(Correo(
            sistema=NOMBRE_MODULO.get(e.modulo, e.modulo), tipo="Alerta",
            evidencia="Registrado", dia=cuando.date(), hora=_hora_limpia(cuando.time()),
            empresa=e.empresa, sede=e.sede,
            detalle=f"{e.punto} · {e.tipo}", n=1,
            destinatarios=e.destinatarios, nota=nota, frecuencia="por evento",
        ))

    # 1b) Alertas ambientales con correo: humedad y temperatura en Sanna.
    # La base no guarda el envío ni los destinatarios; la regla está confirmada
    # por el negocio (CO₂ no genera correo). Se marca como Programado.
    for l in limites:
        if (l.empresa or "").strip().casefold() != "sanna":
            continue
        if l.indicador not in INDICADORES_CON_CORREO:
            continue
        out.append(Correo(
            sistema="Ambiental", tipo="Alerta (humedad/temperatura)",
            evidencia="Programado", dia=l.cuando.date(),
            hora=_hora_limpia(l.cuando.time()),
            empresa=l.empresa, sede=l.sede,
            detalle=f"{l.sala} · {l.equipo} · {etiqueta_nivel(l.nivel)}",
            n=1, destinatarios=DEST_AMBIENTAL,
            nota="regla Sanna: humedad/temperatura",
            frecuencia="por evento",
        ))

    # 2) Resumen diario de alertas (registrado: email_summary_reported_at).
    for r in consultar("energia", SQL_RESUMENES, ini=inicio_dia(d1), fin=fin_dia(d2)):
        m = r._mapping
        momento = _lima(m["momento"])
        out.append(Correo(
            sistema="Energía", tipo="Resumen diario de alertas", evidencia="Registrado",
            dia=momento.date(), hora=_hora_limpia(momento.time()),
            empresa="(global)", sede="",
            detalle=f"{plural(int(m['n']), 'alerta')} incluidas",
            n=1, destinatarios=(), frecuencia=f"diaria · {momento:%H:%M}",
            nota="destinatarios no registrados",
        ))

    # 3) Reporte de estado actual (programado: 1 correo por punto y horario).
    if not config.get("sin_programados"):
        estado = [r._mapping for r in consultar("energia", SQL_ESTADO_ACTUAL)
                  if r._mapping["is_active"] and not _es_prueba(r._mapping["empresa"])]
        for dia in dias:
            grupos = {}
            for m in estado:
                hora = m["report_time"]
                if hora is None:
                    continue
                tipo = ("Estado actual (apertura)" if m["should_be_on"]
                        else "Estado actual (cierre)")
                g = grupos.setdefault((hora, tipo, m["empresa"], m["sede"]),
                                      {"puntos": [], "recips": set(), "notas": set()})
                g["puntos"].append(m["punto"])
                g["recips"].update(parse_destinatarios(m["recips"]))
                if m["descripcion"]:
                    g["notas"].add(m["descripcion"])
            for (hora, tipo, empresa, sede), g in grupos.items():
                # Un solo correo por horario: incluye el estado de todos los
                # puntos (18 en Oechsle Salaverry), no uno por punto.
                out.append(Correo(
                    sistema="Energía", tipo=tipo, evidencia="Programado",
                    dia=dia, hora=_hora_limpia(hora), empresa=empresa, sede=sede,
                    detalle=f'{len(g["puntos"])} puntos: '
                            f'{_puntos_texto(g["puntos"])}',
                    n=1,
                    destinatarios=tuple(sorted(g["recips"])),
                    nota=" · ".join(sorted(g["notas"])),
                    frecuencia=f"diaria · {hora:%H:%M}",
                ))

        # 4) Cobertura diaria (programada: 1 correo por empresa y día, 05:00).
        # El correo reporta la cobertura de datos del día anterior.
        diario = [r._mapping for r in consultar("energia", SQL_PROGRAMADOS_DIARIO)
                  if r._mapping["is_active"] and not _es_prueba(r._mapping["empresa"])]
        for dia in dias:
            ayer = dia - timedelta(days=1)
            for m in diario:
                datos = (cobertura or {}).get((m["empresa"], ayer))
                if datos:
                    detalle = (f"{fmt_dec(datos['pct'], 1)}% de cobertura del "
                               f"{fmt_fecha(ayer)} · "
                               f"{fmt_n(datos['lecturas'])} lecturas en "
                               f"{fmt_n(datos['puntos'])} puntos")
                else:
                    detalle = f"sin datos de cobertura del {fmt_fecha(ayer)}"
                out.append(Correo(
                    sistema="Energía", tipo="Cobertura diaria", evidencia="Programado",
                    dia=dia, hora=time(5, 0), empresa=m["empresa"], sede="",
                    detalle=detalle, n=1,
                    destinatarios=parse_destinatarios(m["recips"]),
                    nota="cobertura del día anterior", frecuencia="diaria · 05:00",
                ))

        # 5) Consumo mensual (programado: solo si el día 1 cae en el rango).
        mensual = [r._mapping for r in consultar("energia", SQL_PROGRAMADOS_MENSUAL)
                   if r._mapping["is_active"] and not _es_prueba(r._mapping["empresa"])]
        for dia in dias:
            if dia.day != 1:
                continue
            for m in mensual:
                out.append(Correo(
                    sistema="Energía", tipo="Consumo mensual", evidencia="Programado",
                    dia=dia, hora=None, empresa=m["empresa"], sede="",
                    detalle="", n=1, destinatarios=parse_destinatarios(m["recips"]),
                    nota="sin hora fija", frecuencia="mensual",
                ))

    # 6) Sin datos ambiental (inferido de los huecos de lecturas).
    for h in huecos:
        estado = "abierto" if h.abierto else "cerrado"
        if h.sin_lecturas:
            estado = "nunca reportó"
        out.append(Correo(
            sistema="Ambiental", tipo="Sin datos", evidencia="Inferido",
            dia=h.inicio.date(), hora=_hora_limpia(h.inicio.time()),
            empresa=h.empresa, sede=h.sede,
            detalle=f"{h.sala} · {h.equipo}", n=1,
            destinatarios=h.destinatarios,
            nota=f"{fmt_duracion(h.minutos)} sin lecturas · {estado}",
            frecuencia="por evento",
        ))

    return sorted(out, key=lambda c: (c.dia, c.hora or time.max,
                                      c.evidencia, c.sistema, c.tipo))


def totales_correos(correos: Sequence[Correo]) -> dict:
    tot = Counter()
    for c in correos:
        tot[c.evidencia] += c.n
    tot["total"] = sum(tot.values())
    return dict(tot)


def correos_por_tipo(correos: Sequence[Correo]) -> list:
    """Agrupa por tipo para la tabla de la portada."""
    agg = {}
    for c in correos:
        clave = (c.evidencia, c.sistema, c.tipo)
        g = agg.setdefault(clave, {"n": 0, "empresas": [], "dest": set(),
                                   "frec": set(), "notas": set()})
        g["n"] += c.n
        if c.empresa and c.empresa not in g["empresas"]:
            g["empresas"].append(c.empresa)
        g["dest"].update(c.destinatarios)
        if c.frecuencia:
            g["frec"].add(c.frecuencia)
        if c.nota:
            g["notas"].add(c.nota)
    orden = {"Registrado": 0, "Programado": 1, "Inferido": 2}
    filas = []
    for (evidencia, sistema, tipo), g in agg.items():
        filas.append({
            "evidencia": evidencia, "sistema": sistema, "tipo": tipo, "n": g["n"],
            "empresas": ", ".join(g["empresas"]),
            "destinatarios": tuple(sorted(g["dest"])),
            "frecuencia": ", ".join(sorted(g["frec"])),
            "nota": ", ".join(sorted(g["notas"])),
        })
    return sorted(filas, key=lambda f: (orden.get(f["evidencia"], 9), f["sistema"], -f["n"]))


def agrupar_umbrales(rows) -> list:
    agg = {}
    for r in rows:
        m = r._mapping
        recips = parse_destinatarios(m["recips"])
        clave = (m["empresa"], recips, bool(m["email_sending"]) and bool(recips))
        agg.setdefault(clave, Counter())[etiqueta_tipo(m["alert_type"])] += 1
    out = []
    for (empresa, recips, correo), tipos in agg.items():
        out.append(ConfigUmbral(
            empresa=empresa, destinatarios=recips, correo=correo,
            tipos=", ".join(f"{k} ({v})" for k, v in tipos.most_common()),
            n=sum(tipos.values()),
        ))
    return sorted(out, key=lambda c: (c.empresa, not c.correo, -c.n))


def agrupar_programados(status, diario, mensual) -> list:
    agg = {}
    for r in status:
        m = r._mapping
        hora = m["report_time"].strftime("%H:%M") if m["report_time"] else "—"
        cuando = f"{hora} · {m['descripcion']}".strip(" ·")
        clave = (m["empresa"], "Resumen apertura/cierre", cuando,
                 parse_destinatarios(m["recips"]), bool(m["is_active"]))
        agg[clave] = agg.get(clave, 0) + 1
    for r in diario:
        m = r._mapping
        cuando = "diario" + (f" · {m['descripcion']}" if m["descripcion"] else "")
        clave = (m["empresa"], "Cobertura diaria", cuando,
                 parse_destinatarios(m["recips"]), bool(m["is_active"]))
        agg[clave] = agg.get(clave, 0) + 1
    for r in mensual:
        m = r._mapping
        cuando = "mensual" + (f" · {m['descripcion']}" if m["descripcion"] else "")
        clave = (m["empresa"], "Consumo mensual", cuando,
                 parse_destinatarios(m["recips"]), bool(m["is_active"]))
        agg[clave] = agg.get(clave, 0) + 1
    return [ConfigProgramado(empresa=e, tipo=t, cuando=c, destinatarios=d,
                             activo=a, n=n)
            for (e, t, c, d, a), n in agg.items()]


def cargar_config() -> dict:
    umbrales = agrupar_umbrales(consultar("energia", SQL_UMBRALES))
    programados = agrupar_programados(
        consultar("energia", SQL_PROGRAMADOS_STATUS),
        consultar("energia", SQL_PROGRAMADOS_DIARIO),
        consultar("energia", SQL_PROGRAMADOS_MENSUAL),
    )
    nodata = []
    for r in consultar("energia", SQL_NODATA_ENERGIA):
        m = r._mapping
        if _es_prueba(m["empresa"]):
            continue
        nodata.append({
            "punto": m["punto"], "empresa": m["empresa"], "sede": m["sede"] or "—",
            "timeout": int(m["timeout_minutes"] or 60),
            "recordatorio": int(m["reminder_minutes"] or 0),
            "correo": parse_destinatarios(m["recips"]),
            "activo": bool(m["is_active"]), "estado": (m["status"] or "").strip(),
            "caido_desde": _lima(m["down_since"]) if m["down_since"] else None,
            "ultimo_aviso": _lima(m["alert_sent_at"]) if m["alert_sent_at"] else None,
            "abierto": (m["status"] or "").strip().lower() not in ("", "ok"),
        })
    ambiental = []
    for r in consultar("ambiental", SQL_AMBIENTAL_DEVICES):
        m = r._mapping
        if _es_prueba(m["empresa"]):
            continue
        ambiental.append({
            "equipo": f"{m['tipo_sensor']} #{m['id']}",
            "empresa": m["empresa"], "sede": m["sede"] or "—", "sala": m["sala"],
            "timeout": int(m["timeout_minutes"] or 60),
            "recordatorio": int(m["reminder_minutes"] or 0),
            "correo": parse_destinatarios(m["recips"]),
            "estado": (m["no_data_status"] or "").strip(),
            "caido_desde": _lima(m["down_started_at"]) if m["down_started_at"] else None,
            "ultimo_aviso": (_lima(m["last_missing_alert_sent"])
                             if m["last_missing_alert_sent"] else None),
            "activo": bool(m["is_activated"]) and m["sala_activa"] is not False,
            "abierto": (m["no_data_status"] or "").strip().lower() not in ("", "ok"),
        })
    ultimo = consultar("energia", SQL_ULTIMO_RESUMEN)
    ultimo_resumen = _lima(ultimo[0][0]) if ultimo and ultimo[0][0] else None
    return {"umbrales": umbrales, "programados": programados,
            "nodata": nodata, "ambiental": ambiental,
            "ultimo_resumen": ultimo_resumen}


def contar_periodo(d1: date, d2: date, empresas: Optional[Sequence[str]] = None) -> dict:
    """Conteo de alertas registradas en un periodo (comparativa).

    Excluye las empresas de prueba, para que la comparación use el mismo
    universo que el detalle del reporte. Si se pasan `empresas`, cuenta solo
    esas (mismo filtro que el reporte).
    """
    ini, fin = inicio_dia(d1), fin_dia(d2)
    pedidas = {(e or "").strip().casefold() for e in empresas} if empresas else None

    def total(base: str, sql: str) -> int:
        n = 0
        for r in consultar(base, sql, ini=ini, fin=fin):
            m = r._mapping
            if _es_prueba(m["empresa"]):
                continue
            if pedidas and (m["empresa"] or "").strip().casefold() not in pedidas:
                continue
            n += int(m["n"])
        return n

    return {
        "energia": total("energia", """
            SELECT COALESCE(e.name, '(sin empresa)') AS empresa, count(*) AS n
            FROM alerts_alert a
            JOIN alerts_alertthreshold t ON t.id = a.alert_threshold_id
            LEFT JOIN enterprises_enterprise e ON e.id = t.enterprise_id
            WHERE a."timestamp" >= :ini AND a."timestamp" < :fin
            GROUP BY 1
        """),
        "agua": total("energia", """
            SELECT COALESCE(e_t.name, e_w.name, '(sin empresa)') AS empresa, count(*) AS n
            FROM alerts_wateralert w
            JOIN alerts_alertthreshold t ON t.id = w.alert_threshold_id
            LEFT JOIN enterprises_enterprise e_t ON e_t.id = t.enterprise_id
            LEFT JOIN enterprises_measurementpointwater mw
                   ON mw.id = t.measurement_point_water_id
            LEFT JOIN enterprises_waterpipe wp ON wp.id = mw.water_pipe_id
            LEFT JOIN enterprises_energyheadquarter eh_w
                   ON eh_w.id = wp.energy_headquarter_id
            LEFT JOIN enterprises_enterprise e_w ON e_w.id = eh_w.enterprise_id
            WHERE w."timestamp" >= :ini AND w."timestamp" < :fin
            GROUP BY 1
        """),
        "ambiental": total("ambiental", """
            SELECT COALESCE(e.name, '(sin empresa)') AS empresa, count(*) AS n
            FROM alerts_limitalert a
            LEFT JOIN enterprise_room r ON r.id = a.room_id
            LEFT JOIN enterprise_headquarters h ON h.id = r.headquarter_id
            LEFT JOIN enterprise_enterprise e ON e.id = h.enterprise_id
            WHERE a.created_at >= :ini AND a.created_at < :fin
            GROUP BY 1
        """),
    }


def comparativa(d1: date, d2: date, actual: dict,
                empresas: Optional[Sequence[str]] = None) -> list:
    """Periodo del reporte + mismo largo inmediatamente antes + hace 4 semanas."""
    largo = (d2 - d1).days + 1
    periodos = [
        ("Periodo del reporte", d1, d2, actual),
        ("Periodo anterior", d1 - timedelta(days=largo), d2 - timedelta(days=largo), None),
        ("4 semanas antes", d1 - timedelta(days=28), d2 - timedelta(days=28), None),
    ]
    out = []
    for etiqueta, a, b, datos in periodos:
        if b < date(2020, 1, 1):
            continue
        datos = datos or contar_periodo(a, b, empresas)
        total = datos["energia"] + datos["agua"] + datos["ambiental"]
        out.append({
            "etiqueta": f"{etiqueta}: {fmt_fecha_corta(a)}–{fmt_fecha_corta(b)}",
            "energia": datos["energia"], "agua": datos["agua"],
            "ambiental": datos["ambiental"], "total": total,
        })
    return out


# ---------- Agregaciones ------------------------------------------------------

def filtrar_empresas(items, empresas: Optional[Sequence[str]]):
    if not empresas:
        return list(items)
    pedidas = {(e or "").strip().casefold() for e in empresas}
    return [x for x in items if (getattr(x, "empresa", "") or "").strip().casefold() in pedidas]


def _filtrar_config(config: dict, empresas: Sequence[str]) -> dict:
    """Mismo filtro de empresas para el anexo de configuración."""
    pedidas = {(e or "").strip().casefold() for e in empresas}

    def ok(x) -> bool:
        emp = x.empresa if hasattr(x, "empresa") else x.get("empresa", "")
        return (emp or "").strip().casefold() in pedidas

    return {clave: ([x for x in valor if ok(x)] if isinstance(valor, list) else valor)
            for clave, valor in config.items()}


def totales(eventos: Sequence[Evento], limites: Sequence[Limite],
            episodios: Sequence[Episodio], grupos: Sequence[GrupoLimite],
            huecos: Sequence[Hueco]) -> dict:
    dest = set()
    for e in eventos:
        dest.update(e.destinatarios)
    puntos = {f"{e.empresa}|{e.punto}" for e in eventos}
    puntos |= {f"{l.empresa}|{l.sala}|{l.equipo}" for l in limites}
    empresas = {e.empresa for e in eventos} | {l.empresa for l in limites} | {h.empresa for h in huecos}
    sedes = {f"{e.empresa}|{e.sede}" for e in eventos} | {f"{l.empresa}|{l.sede}" for l in limites}
    resumen = sum(1 for e in eventos if e.resumen)
    dias_resumen = {e.resumen_cuando.date() for e in eventos if e.resumen_cuando}
    return {
        "alertas": len(eventos) + len(limites),
        "alertas_energia": sum(1 for e in eventos if e.modulo == "energia"),
        "alertas_agua": sum(1 for e in eventos if e.modulo == "agua"),
        "alertas_ambiental": len(limites),
        "episodios": len(episodios) + len(grupos),
        "episodios_energia": sum(1 for e in episodios if e.modulo == "energia"),
        "episodios_agua": sum(1 for e in episodios if e.modulo == "agua"),
        "grupos_ambiental": len(grupos),
        "criticas": (sum(1 for e in eventos if e.severidad == "Crítica")
                     + sum(1 for l in limites if severidad_ambiental(l.nivel) == "Crítica")),
        "empresas": len(empresas),
        "sedes": len(sedes),
        "puntos": len(puntos),
        "destinatarios": len(dest),
        "con_correo": sum(1 for e in eventos if e.correo_activo is True),
        "sin_correo": sum(1 for e in eventos if e.correo_activo is False),
        "notificadas": sum(1 for e in eventos if e.reportada),
        "resumen": resumen,
        "dias_resumen": len(dias_resumen),
        "huecos": len(huecos),
    }


def resumen_empresas(eventos: Sequence[Evento], limites: Sequence[Limite],
                     episodios: Sequence[Episodio], grupos: Sequence[GrupoLimite]) -> list:
    ev = Counter((e.empresa, e.modulo) for e in eventos)
    ep = Counter((e.empresa, e.modulo) for e in episodios)
    lim = Counter(l.empresa for l in limites)
    grp = Counter(g.empresa for g in grupos)
    criticas = Counter()
    for e in eventos:
        if e.severidad == "Crítica":
            criticas[e.empresa] += 1
    for l in limites:
        if severidad_ambiental(l.nivel) == "Crítica":
            criticas[l.empresa] += 1
    puntos = defaultdict(set)
    dest = defaultdict(set)
    for e in eventos:
        puntos[e.empresa].add(f"E|{e.punto}")
        dest[e.empresa].update(e.destinatarios)
    for l in limites:
        puntos[l.empresa].add(f"A|{l.sala}|{l.equipo}")
    empresas = sorted({e.empresa for e in eventos} | {l.empresa for l in limites})
    filas = []
    for empresa in empresas:
        energia = ev.get((empresa, "energia"), 0)
        agua = ev.get((empresa, "agua"), 0)
        filas.append({
            "empresa": empresa,
            "energia": energia, "episodios_energia": ep.get((empresa, "energia"), 0),
            "agua": agua, "episodios_agua": ep.get((empresa, "agua"), 0),
            "ambiental": lim.get(empresa, 0), "grupos_ambiental": grp.get(empresa, 0),
            "total": energia + agua + lim.get(empresa, 0),
            "criticas": criticas.get(empresa, 0),
            "puntos": len(puntos.get(empresa, set())),
            "destinatarios": len(dest.get(empresa, set())),
        })
    filas.sort(key=lambda f: (-f["total"], f["empresa"]))
    return filas


def conteo_por_dia(eventos: Sequence[Evento], limites: Sequence[Limite],
                   dias: Sequence[date]) -> dict:
    out = {m: [0] * len(dias) for m in MODULOS}
    for e in eventos:
        idx = (e.cuando.date() - dias[0]).days
        if 0 <= idx < len(dias):
            out[e.modulo][idx] += 1
    for l in limites:
        idx = (l.cuando.date() - dias[0]).days
        if 0 <= idx < len(dias):
            out["ambiental"][idx] += 1
    return out


def conteo_tipos(eventos: Sequence[Evento], limites: Sequence[Limite]) -> list:
    c = Counter()
    for e in eventos:
        c[e.tipo] += 1
    for l in limites:
        c[f"{l.indicador}: {etiqueta_nivel(l.nivel)}"] += 1
    return c.most_common(8)


def hallazgos(eventos: Sequence[Evento], limites: Sequence[Limite],
              grupos: Sequence[GrupoLimite]) -> dict:
    """Listas cortas para la sección "lo que conviene mirar"."""
    por_punto = Counter()
    for e in eventos:
        por_punto[f"{e.empresa} · {e.punto}"] += 1
    for l in limites:
        por_punto[f"{l.empresa} · {l.sala} · {l.equipo}"] += 1
    por_remitente = Counter()
    for e in eventos:
        for d in e.destinatarios:
            por_remitente[d] += 1
    sin_correo = Counter()
    for e in eventos:
        if e.correo_activo is False:
            sin_correo[f"{e.empresa} · {e.punto}"] += 1
    fuera = 0
    for e in eventos:
        minuto = e.cuando.hour * 60 + e.cuando.minute
        if minuto < HORARIO_INI or minuto >= HORARIO_FIN:
            fuera += 1
    for l in limites:
        minuto = l.cuando.hour * 60 + l.cuando.minute
        if minuto < HORARIO_INI or minuto >= HORARIO_FIN:
            fuera += 1
    total = len(eventos) + len(limites)
    return {
        "top_puntos": por_punto.most_common(5),
        "top_destinatarios": por_remitente.most_common(5),
        "sin_correo": sin_correo.most_common(5),
        "n_sin_correo": sum(sin_correo.values()),
        "fuera_horario": fuera,
        "total": total,
        "pct_fuera": (100.0 * fuera / total) if total else 0.0,
        "top_salas": Counter(f"{g.sede} · {g.sala}" for g in grupos).most_common(4),
    }


# ---------- HTML: gráficos ----------------------------------------------------

def chart_dias(dias: Sequence[date], conteos: dict) -> str:
    maxv = max([sum(conteos[m][i] for m in MODULOS) for i in range(len(dias))] + [1])
    cols = []
    for i, d in enumerate(dias):
        total = sum(conteos[m][i] for m in MODULOS)
        segs = "".join(
            f'<div class="seg {CLASE_MODULO[m]}" style="height:{conteos[m][i] / maxv * 100:.1f}%"></div>'
            for m in MODULOS if conteos[m][i]
        )
        cols.append(
            f'<div class="col"><div class="valor-barra">{fmt_n(total) if total else ""}</div>'
            f'<div class="pila">{segs}</div><div class="dia">{fmt_fecha(d)}</div></div>'
        )
    leyenda = "".join(f'<span><span class="punto {CLASE_MODULO[m]}"></span>{NOMBRE_MODULO[m]}</span>'
                      for m in MODULOS)
    return (f'<div class="chart"><div class="chart-h">Alertas por día</div>'
            f'<div class="leyenda">{leyenda}</div><div class="barras">{"".join(cols)}</div></div>')


CATEGORIA_CORREO = {
    "Alerta": "alertas",
    "Resumen diario de alertas": "resumen",
    "Sin datos": "sindatos",
}


def categoria_correo(c: Correo) -> str:
    if c.tipo.startswith("Alerta"):
        return "alertas"
    return CATEGORIA_CORREO.get(c.tipo, "reportes")


def chart_correos_dia(dias: Sequence[date], correos: Sequence[Correo]) -> str:
    """Barras apiladas: cuántos correos salieron cada día y de qué tipo."""
    orden = ["alertas", "reportes", "sindatos", "resumen"]
    nombre = {"alertas": "Alertas", "reportes": "Reportes",
              "sindatos": "Sin datos", "resumen": "Resumen"}
    idx = {d: i for i, d in enumerate(dias)}
    cat = {k: [0] * len(dias) for k in orden}
    for c in correos:
        if c.dia in idx:
            cat[categoria_correo(c)][idx[c.dia]] += c.n
    totales = [sum(cat[k][i] for k in orden) for i in range(len(dias))]
    maxv = max(totales + [1])
    cols = []
    for i, d in enumerate(dias):
        segs = "".join(
            f'<div class="seg c-{k}" style="height:{cat[k][i] / maxv * 100:.1f}%"></div>'
            for k in orden if cat[k][i]
        )
        cols.append(
            f'<div class="col"><div class="valor-barra">{fmt_n(totales[i]) if totales[i] else ""}</div>'
            f'<div class="pila">{segs}</div><div class="dia">{fmt_fecha(d)}</div></div>'
        )
    leyenda = "".join(f'<span><span class="punto c-{k}"></span>{nombre[k]}</span>'
                      for k in orden)
    return (f'<div class="chart"><div class="chart-h">Correos por día</div>'
            f'<div class="leyenda">{leyenda}</div>'
            f'<div class="barras">{"".join(cols)}</div></div>')


def chart_tipos(items: Sequence[tuple]) -> str:
    if not items:
        return ('<div class="chart"><div class="chart-h">Alertas por tipo</div>'
                '<p class="vacio">Sin alertas.</p></div>')
    maxv = max([n for _, n in items] + [1])
    barras = "".join(
        f'<div class="hbar" title="{esc(nombre)}"><span class="nom">{esc(nombre)}</span>'
        f'<div class="track"><div class="fill" style="width:{n / maxv * 100:.1f}%"></div></div>'
        f'<span class="n">{fmt_n(n)}</span></div>'
        for nombre, n in items
    )
    return (f'<div class="chart"><div class="chart-h">Alertas por tipo</div>'
            f'<div class="hbars">{barras}</div></div>')


# ---------- HTML: tablas y estilos -------------------------------------------

CSS = """
@page { size: letter landscape; margin: 9mm 10mm 10mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; color: #1f2937; font-size: 9px; line-height: 1.32;
       font-family: -apple-system, "Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif; }
section.semana, section.anexo, section.correos, section.alertas { break-before: page; }
header.doc { border-bottom: 2px solid #111827; padding-bottom: 6px; margin-bottom: 7px; }
h1 { font-size: 20px; margin: 0; color: #111827; }
h2 { font-size: 14px; margin: 0; color: #111827; }
h3 { font-size: 11px; margin: 9px 0 3px; color: #111827; }
h4 { font-size: 9px; margin: 0 0 3px; }
.sub { color: #6b7280; font-size: 9px; }
.meta { display: flex; gap: 18px; flex-wrap: wrap; font-size: 8.8px; color: #374151; margin-top: 4px; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 10px 0; }
.kpis.correo { grid-template-columns: repeat(4, 1fr); margin-top: 4px; }
.kpi { border: 1px solid #e5e7eb; border-radius: 6px; padding: 6px 9px; background: #f8fafc; }
.kpi .v { font-size: 20px; font-weight: 800; color: #111827; line-height: 1.1; }
.kpi .l { font-size: 7.8px; color: #6b7280; text-transform: uppercase; letter-spacing: .3px; }
.kpi .s { font-size: 7.8px; color: #374151; margin-top: 2px; }
.nota-caja { border-left: 3px solid #2563eb; background: #eff6ff; padding: 6px 9px;
             margin: 8px 0; font-size: 8.6px; }
.nota-caja b { color: #1e3a8a; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
thead { display: table-header-group; }
th { background: #1f2937; color: #fff; font-weight: 600; font-size: 7.8px; padding: 3px;
     text-align: left; border: 1px solid #1f2937; }
td { border: 1px solid #e5e7eb; padding: 2.4px 3px; vertical-align: top; font-size: 8.1px; }
tr.grupo td { background: #eef2f7; color: #334155; font-weight: 700; font-size: 8.3px; padding: 2px 4px; }
tr.total td { background: #f1f5f9; font-weight: 700; border-top: 2px solid #94a3b8; }
tr { break-inside: avoid; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.centro { text-align: center; }
.badge { display: inline-block; padding: 0 3px; border-radius: 3px; font-size: 7.1px;
         font-weight: 700; white-space: nowrap; }
.critica { background: #fee2e2; color: #b91c1c; }
.alta { background: #ffedd5; color: #c2410c; }
.media { background: #fef3c7; color: #b45309; }
.baja { background: #e2e8f0; color: #475569; }
.si { color: #047857; font-weight: 700; }
.no { color: #b91c1c; font-weight: 700; }
.na { color: #9ca3af; }
.sub2 { color: #6b7280; font-size: 7.3px; }
.mail { font-size: 7.3px; word-break: break-word; }
.nota { font-size: 7.5px; color: #4b5563; }
.vacio { color: #6b7280; font-style: italic; padding: 3px 0; }
.charts { display: grid; grid-template-columns: 1fr 1.2fr 1fr; gap: 8px; margin: 5px 0 7px; }
.charts.dos { grid-template-columns: 1fr 1fr; }
.chart { border: 1px solid #e5e7eb; border-radius: 6px; padding: 6px 8px; break-inside: avoid; }
.chart-h { font-weight: 700; font-size: 9px; margin-bottom: 4px; }
.leyenda { display: flex; gap: 10px; font-size: 7.4px; color: #4b5563; margin-bottom: 2px; }
.punto { display: inline-block; width: 7px; height: 7px; border-radius: 2px; margin-right: 3px; }
.e { background: #2563eb; } .a { background: #0891b2; } .amb { background: #7c3aed; }
.c-alertas { background: #2563eb; } .c-reportes { background: #0f766e; }
.c-sindatos { background: #b45309; } .c-resumen { background: #7c3aed; }
.barras { display: flex; align-items: flex-end; gap: 5px; height: 100px;
          border-bottom: 1px solid #d1d5db; }
.col { flex: 1; display: flex; flex-direction: column; justify-content: flex-end;
       align-items: center; height: 100%; }
.pila { width: 100%; display: flex; flex-direction: column; justify-content: flex-end;
        height: 82px; }
.seg { width: 100%; }
.valor-barra { font-size: 7px; color: #374151; }
.dia { font-size: 7px; color: #6b7280; margin-top: 2px; white-space: nowrap; }
.hbars { display: flex; flex-direction: column; gap: 3px; }
.hbar { display: flex; align-items: center; gap: 4px; font-size: 7.6px; }
.hbar .nom { width: 38%; color: #374151; overflow: hidden; text-overflow: ellipsis;
             white-space: nowrap; }
.hbar .track { flex: 1; background: #f1f5f9; border-radius: 2px; height: 7px; overflow: hidden; }
.hbar .fill { height: 100%; background: #2563eb; }
.hbar .n { width: 8%; text-align: right; font-variant-numeric: tabular-nums; }
.hallazgos { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.hallazgos.tres { grid-template-columns: repeat(3, 1fr); }
.panel-2 { display: grid; grid-template-columns: 40% 1fr; gap: 8px; align-items: start; margin: 6px 0; }
.indice { font-size: 8.2px; color: #475569; margin-top: 5px; }
.indice b { color: #111827; }
.hallazgo { border: 1px solid #e5e7eb; border-radius: 6px; padding: 6px 8px; break-inside: avoid; }
.hallazgo ul { margin: 0; padding-left: 13px; }
.hallazgo li { margin: 1px 0; font-size: 8px; }
"""


def _tabla(cols: Sequence[int], head: Sequence[str], cuerpo: Sequence[str],
           clase: str = "") -> str:
    colgroup = "".join(f'<col style="width:{w}%">' for w in cols)
    thead = "".join(f"<th>{esc(h)}</th>" for h in head)
    return (f'<table class="{clase}"><colgroup>{colgroup}</colgroup>'
            f'<thead><tr>{thead}</tr></thead><tbody>{"".join(cuerpo)}</tbody></table>')


def _badge(sev: str) -> str:
    clase = {"Crítica": "critica", "Alta": "alta", "Media": "media",
             "Moderada": "media", "Baja": "baja", "Alerta": "media"}.get(sev, "media")
    return f'<span class="badge {clase}">{esc(sev)}</span>'


def _correo_celda(correo: Optional[bool]) -> str:
    if correo is True:
        return '<span class="si">Sí</span>'
    if correo is False:
        return '<span class="no">No</span>'
    return '<span class="na">—</span>'


def _dest_celda(dest: Sequence[str], correo: Optional[bool],
                tope: int = 0) -> str:
    if not dest:
        return '<span class="no">Sin destinatarios</span>'
    if tope and len(dest) > tope:
        txt = (f'<span class="mail">{esc(", ".join(dest[:tope]))} '
               f'y {fmt_n(len(dest) - tope)} más</span>')
    else:
        txt = f'<span class="mail">{esc(", ".join(dest))}</span>'
    if correo is False:
        txt += ' <span class="no">(desactivado)</span>'
    return txt


def _notif_celda(notif: str) -> str:
    clase = {"Sí": "si", "Parcial": "media"}.get(notif, "na")
    return f'<span class="{clase}">{esc(notif)}</span>'


def _resumen_celda(n: int, total: int) -> str:
    if not n:
        return '<span class="na">—</span>'
    if total and n >= total:
        return '<span class="si">Sí</span>'
    return f'<span class="media">{fmt_n(n)}/{fmt_n(total)}</span>'


def _tabla_episodios(episodios: Sequence[Episodio], titulo: str,
                     subtitulo: str = "") -> str:
    if not episodios:
        return (f'<h3>{esc(titulo)} <span class="sub">· {esc(subtitulo)}</span></h3>'
                '<p class="vacio">Sin alertas registradas en el periodo.</p>')
    head = ["Fecha", "Hora", "Tablero / tubería", "Punto / medidor", "Tipo", "Sev.",
            "N", "Valor", "Descripción", "Destinatarios", "Notif.", "Resumen", "Correo"]
    cols = [6, 8, 11, 12, 11, 4, 3, 5, 16, 12, 3, 4, 5]
    cuerpo = []
    grupos = defaultdict(list)
    for e in episodios:
        grupos[(e.empresa, e.sede)].append(e)
    for (empresa, sede), items in grupos.items():
        n_alertas = sum(x.n for x in items)
        cuerpo.append(f'<tr class="grupo"><td colspan="13">{esc(empresa)} · {esc(sede)} '
                      f'— {plural(len(items), "episodio")} · '
                      f'{plural(n_alertas, "alerta")}</td></tr>')
        for e in items:
            tipo = esc(e.tipo)
            if e.subtipo:
                tipo += f'<br><span class="sub2">{esc(e.subtipo)}</span>'
            cuerpo.append(
                f'<tr><td>{fmt_fecha(e.inicio.date())}</td>'
                f'<td>{fmt_rango_horas(e.inicio, e.fin)}</td>'
                f'<td>{esc(e.grupo)}</td>'
                f'<td>{esc(e.punto)}</td>'
                f'<td>{tipo}</td>'
                f'<td class="centro">{_badge(e.severidad)}</td>'
                f'<td class="num">{fmt_n(e.n)}</td>'
                f'<td class="num">{fmt_valor(e.valor_min, e.valor_max, e.unidad)}</td>'
                f'<td><span class="nota">{esc(_recorta(e.nota, 200))}</span></td>'
                f'<td>{_dest_celda(e.destinatarios, e.correo_activo)}</td>'
                f'<td class="centro">{_notif_celda(e.notificacion)}</td>'
                f'<td class="centro">{_resumen_celda(e.resumen, e.n)}</td>'
                f'<td class="centro">{_correo_celda(e.correo_activo)}</td></tr>'
            )
    return (f'<h3>{esc(titulo)} <span class="sub">· {esc(subtitulo)}</span></h3>'
            + _tabla(cols, head, cuerpo, "detalle"))


def _tabla_limites(grupos: Sequence[GrupoLimite], dias: Sequence[date]) -> str:
    if not grupos:
        return '<h3>Ambiental — Limit Alerts</h3><p class="vacio">Sin alertas de límite ambiental.</p>'
    head = ["Equipo", "Indicador", "Nivel", "N", "Lun–Dom", "Primera", "Última",
            "Valor", "Medición", "Resueltas", "Correo"]
    cols = [14, 10, 13, 5, 14, 9, 9, 8, 8, 6, 4]
    cuerpo = []
    por_sala = defaultdict(list)
    for g in grupos:
        por_sala[(g.empresa, g.sede, g.sala)].append(g)
    for (empresa, sede, sala), items in por_sala.items():
        n_alertas = sum(x.n for x in items)
        cuerpo.append(f'<tr class="grupo"><td colspan="11">{esc(empresa)} · {esc(sede)} · '
                      f'{esc(sala)} — {fmt_n(n_alertas)} alertas</td></tr>')
        for g in items:
            dias_txt = "·".join(str(x) for x in g.dias)
            medicion = ", ".join(f"{k} ({v})" for k, v in g.mediciones.most_common())
            cuerpo.append(
                f'<tr><td>{esc(g.equipo)}</td>'
                f'<td>{esc(g.indicador)}</td>'
                f'<td>{_badge(severidad_ambiental(g.nivel))} {esc(etiqueta_nivel(g.nivel))}</td>'
                f'<td class="num">{fmt_n(g.n)}</td>'
                f'<td class="centro">{dias_txt}</td>'
                f'<td>{fmt_fecha_hora(g.primera) if g.primera else "—"}</td>'
                f'<td>{fmt_fecha_hora(g.ultima) if g.ultima else "—"}</td>'
                f'<td class="num">{fmt_valor(g.valor_min, g.valor_max, g.unidad)}</td>'
                f'<td><span class="sub2">{esc(medicion)}</span></td>'
                f'<td class="centro">{fmt_n(g.resueltas)}/{fmt_n(g.n)}</td>'
                f'<td class="centro"><span class="na">No aplica</span></td></tr>'
            )
    return ('<h3>Ambiental — Limit Alerts <span class="sub">· alertas de límite '
            'registradas por la plataforma (no generan correo)</span></h3>'
            + _tabla(cols, head, cuerpo, "detalle"))


def _tabla_huecos(huecos: Sequence[Hueco]) -> str:
    if not huecos:
        return ('<h3>Ambiental — correos de sin datos <span class="sub">· inferido</span></h3>'
                '<p class="vacio">Sin episodios sin datos ≥ 60 min en el periodo.</p>')
    head = ["Sala", "Equipo", "Inicio", "Fin", "Duración", "Estado",
            "Destinatarios", "Correo"]
    cols = [15, 14, 12, 12, 8, 9, 22, 8]
    cuerpo = []
    por_sala = defaultdict(list)
    for h in huecos:
        por_sala[(h.empresa, h.sede, h.sala)].append(h)
    for (empresa, sede, sala), items in por_sala.items():
        cuerpo.append(f'<tr class="grupo"><td colspan="8">{esc(empresa)} · {esc(sede)} · '
                      f'{esc(sala)} — {plural(len(items), "episodio")}</td></tr>')
        for h in items:
            estado = "Abierto" if h.abierto else "Cerrado"
            if h.sin_lecturas:
                estado = "Sin lecturas"
            elif h.venia_de_antes:
                estado += " · venía de antes"
            cuerpo.append(
                f'<tr><td>{esc(h.sala)}</td><td>{esc(h.equipo)}</td>'
                f'<td>{fmt_fecha_hora(h.inicio)}</td><td>{fmt_fecha_hora(h.fin)}</td>'
                f'<td class="num">{fmt_duracion(h.minutos)}</td>'
                f'<td>{esc(estado)}</td>'
                f'<td>{_dest_celda(h.destinatarios, h.correo_activo)}</td>'
                f'<td class="centro">{_correo_celda(h.correo_activo)}</td></tr>'
            )
    return ('<h3>Ambiental — correos de sin datos <span class="sub">· INFERIDO: '
            'la base no guarda el histórico de estos correos</span></h3>'
            + _tabla(cols, head, cuerpo, "detalle"))


def _tabla_resumen(filas: Sequence[dict]) -> str:
    head = ["Empresa", "Energía", "Agua", "Ambiental", "Total", "Críticas",
            "Puntos", "Destinatarios"]
    cols = [20, 13, 11, 15, 8, 8, 7, 18]
    cuerpo = []
    for f in filas:
        def celda(n, ep):
            if not n:
                return '<span class="na">—</span>'
            return f'{fmt_n(n)} <span class="sub2">/ {fmt_n(ep)} ep.</span>'
        cuerpo.append(
            f'<tr><td>{esc(f["empresa"])}</td>'
            f'<td class="num">{celda(f["energia"], f["episodios_energia"])}</td>'
            f'<td class="num">{celda(f["agua"], f["episodios_agua"])}</td>'
            f'<td class="num">{celda(f["ambiental"], f["grupos_ambiental"])}</td>'
            f'<td class="num"><b>{fmt_n(f["total"])}</b></td>'
            f'<td class="num">{fmt_n(f["criticas"])}</td>'
            f'<td class="num">{fmt_n(f["puntos"])}</td>'
            f'<td class="num">{fmt_n(f["destinatarios"])}</td></tr>'
        )
    tot = {
        "energia": sum(f["energia"] for f in filas),
        "ep_energia": sum(f["episodios_energia"] for f in filas),
        "agua": sum(f["agua"] for f in filas),
        "ep_agua": sum(f["episodios_agua"] for f in filas),
        "ambiental": sum(f["ambiental"] for f in filas),
        "grp_ambiental": sum(f["grupos_ambiental"] for f in filas),
        "total": sum(f["total"] for f in filas),
        "criticas": sum(f["criticas"] for f in filas),
        "puntos": sum(f["puntos"] for f in filas),
        "dest": max([f["destinatarios"] for f in filas] + [0]),
    }
    cuerpo.append(
        f'<tr class="total"><td>Total</td>'
        f'<td class="num">{fmt_n(tot["energia"])} <span class="sub2">/ {fmt_n(tot["ep_energia"])} ep.</span></td>'
        f'<td class="num">{fmt_n(tot["agua"])} <span class="sub2">/ {fmt_n(tot["ep_agua"])} ep.</span></td>'
        f'<td class="num">{fmt_n(tot["ambiental"])} <span class="sub2">/ {fmt_n(tot["grp_ambiental"])} gr.</span></td>'
        f'<td class="num">{fmt_n(tot["total"])}</td><td class="num">{fmt_n(tot["criticas"])}</td>'
        f'<td class="num">{fmt_n(tot["puntos"])}</td><td class="num">{fmt_n(tot["dest"])}</td></tr>'
    )
    return _tabla(cols, head, cuerpo, "resumen")


# ---------- HTML: secciones ---------------------------------------------------

def _kpis(items: Sequence[tuple], clase: str = "") -> str:
    tarjetas = []
    for item in items:
        v, l = item[0], item[1]
        sub = item[2] if len(item) > 2 else ""
        tarjetas.append(
            f'<div class="kpi"><div class="v">{v}</div>'
            f'<div class="l">{esc(l)}</div>'
            + (f'<div class="s">{esc(sub)}</div>' if sub else "")
            + "</div>"
        )
    return f'<div class="kpis {clase}">' + "".join(tarjetas) + "</div>"


def _evidencia_celda(evidencia: str) -> str:
    clase = {"Registrado": "si", "Programado": "media", "Inferido": "na"}.get(evidencia, "na")
    return f'<span class="{clase}">{esc(evidencia)}</span>'



def _tabla_comparativa(filas: Sequence[dict]) -> str:
    """filas = [{etiqueta, energia, agua, ambiental, total}]"""
    head = ["Periodo", "Energía", "Agua", "Ambiental", "Total", "Variación"]
    cols = [34, 12, 10, 14, 12, 18]
    cuerpo = []
    base = filas[0]["total"] if filas else 0
    for i, f in enumerate(filas):
        if i == 0:
            var = '<span class="sub2">periodo del reporte</span>'
        elif base:
            pct = 100.0 * (f["total"] - base) / base
            clase = "no" if pct > 15 else ("si" if pct < -15 else "sub2")
            signo = "+" if pct >= 0 else ""
            var = f'<span class="{clase}">{signo}{fmt_dec(pct, 0)} %</span>'
        else:
            var = '<span class="na">—</span>'
        estilo = ' style="font-weight:700"' if i == 0 else ""
        cuerpo.append(
            f'<tr{estilo}><td>{esc(f["etiqueta"])}</td>'
            f'<td class="num">{fmt_n(f["energia"])}</td>'
            f'<td class="num">{fmt_n(f["agua"])}</td>'
            f'<td class="num">{fmt_n(f["ambiental"])}</td>'
            f'<td class="num"><b>{fmt_n(f["total"])}</b></td>'
            f'<td class="num">{var}</td></tr>'
        )
    return _tabla(cols, head, cuerpo, "comparativa")


def _tabla_correos_tipo(filas: Sequence[dict]) -> str:
    """Tabla de la portada: qué correos hay, con qué evidencia y a quién."""
    head = ["Evidencia", "Sistema", "Tipo de correo", "Frecuencia", "N",
            "Destinatarios", "Empresas"]
    cols = [10, 9, 20, 13, 6, 27, 15]
    cuerpo = []
    for f in filas:
        dest = (_dest_celda(f["destinatarios"], True) if f["destinatarios"]
                else '<span class="na">no registrados</span>')
        cuerpo.append(
            f'<tr><td class="centro">{_evidencia_celda(f["evidencia"])}</td>'
            f'<td>{esc(f["sistema"])}</td>'
            f'<td>{esc(f["tipo"])}</td>'
            f'<td>{esc(f["frecuencia"] or "—")}</td>'
            f'<td class="num"><b>{fmt_n(f["n"])}</b></td>'
            f'<td>{dest}</td>'
            f'<td>{esc(f["empresas"] or "—")}</td></tr>'
        )
    if not cuerpo:
        cuerpo = ['<tr><td colspan="7" class="vacio">Sin correos en el periodo.</td></tr>']
    return _tabla(cols, head, cuerpo, "correos")


def _agrupar_correos(correos: Sequence[Correo]) -> dict:
    """{clave: fila} por día + hora + tipo; suma N y concatena puntos.

    Las alertas se agrupan por día y punto (sin hora) para que el anexo no
    crezca sin control: el detalle con hora exacta está en el anexo A.
    """
    agg = {}
    for c in correos:
        hora = None if c.tipo.startswith("Alerta") else c.hora
        clave = (c.dia, hora, c.evidencia, c.sistema, c.tipo, c.detalle)
        g = agg.setdefault(clave, {"n": 0, "empresas": [], "sedes": [],
                                   "detalle": [], "dest": set(), "notas": [],
                                   "horas": []})
        g["n"] += c.n
        if c.empresa and c.empresa not in g["empresas"]:
            g["empresas"].append(c.empresa)
        if c.sede and c.sede not in g["sedes"]:
            g["sedes"].append(c.sede)
        if c.detalle and c.detalle not in g["detalle"]:
            g["detalle"].append(c.detalle)
        g["dest"].update(c.destinatarios)
        if c.nota and c.nota not in g["notas"]:
            g["notas"].append(c.nota)
        if c.hora is not None:
            g["horas"].append(c.hora)
    return agg


def _hora_texto(hora: Optional[time], g: dict) -> str:
    """Hora de la fila; las agrupadas muestran su rango o su única hora."""
    if hora is not None:
        return hora.strftime("%H:%M")
    horas = set(g.get("horas") or [])
    if not horas:
        return "—"
    if len(horas) == 1:
        return next(iter(horas)).strftime("%H:%M")
    return "varias"


def _fila_correo(hora_txt: str, evidencia: str, sistema: str, tipo: str,
                 g: dict) -> str:
    dest = (_dest_celda(tuple(sorted(g["dest"])), True) if g["dest"]
            else '<span class="na">—</span>')
    empresa = ", ".join(g["empresas"]) or "—"
    if len(g["sedes"]) == 1:
        empresa += f' · {g["sedes"][0]}'
    notas = list(g["notas"])
    horas = g.get("horas") or []
    if hora_txt == "varias" and len(set(horas)) > 1:
        notas.insert(0, f"{min(horas):%H:%M}–{max(horas):%H:%M}")
    return (
        f'<tr><td class="centro">{esc(hora_txt)}</td>'
        f'<td>{esc(tipo)}</td>'
        f'<td>{esc(sistema)}</td>'
        f'<td>{esc(empresa)}</td>'
        f'<td>{esc(", ".join(g["detalle"])) or "—"}</td>'
        f'<td class="num">{fmt_n(g["n"])}</td>'
        f'<td>{dest}</td>'
        f'<td class="nota">{esc(" · ".join(notas)) or "—"}</td>'
        f'<td class="centro">{_evidencia_celda(evidencia)}</td></tr>'
    )


def anexo_correos(dias: Sequence[date], correos: Sequence[Correo]) -> str:
    tot = totales_correos(correos)
    agg = _agrupar_correos(correos)
    orden_ev = {"Registrado": 0, "Programado": 1, "Inferido": 2}
    head = ["Hora", "Tipo", "Sistema", "Empresa · Sede", "Detalle", "N",
            "Destinatarios", "Nota", "Evidencia"]
    cols = [6, 15, 8, 13, 23, 5, 17, 6, 7]
    partes = []
    for dia in dias:
        filas = [(k, v) for k, v in agg.items() if k[0] == dia]
        filas.sort(key=lambda kv: (
            kv[0][1] or (min(kv[1]["horas"]) if kv[1]["horas"] else time.max),
            orden_ev.get(kv[0][2], 9), kv[0][3], kv[0][4], kv[0][5]))
        n_dia = sum(f[1]["n"] for f in filas)
        cuerpo = []
        for (dia_k, hora, evidencia, sistema, tipo, _det), g in filas:
            cuerpo.append(_fila_correo(_hora_texto(hora, g), evidencia, sistema, tipo, g))
        partes.append(
            f'<h3>{fmt_fecha(dia)} <span class="sub">· '
            f'{plural(n_dia, "correo")}</span></h3>'
            + (_tabla(cols, head, cuerpo, "correos") if cuerpo
               else '<p class="vacio">Sin correos este día.</p>')
        )
    # Un correo puede caer fuera del rango: p. ej. una alerta del último día
    # que la plataforma marcó como reportada al día siguiente. No se pierde.
    otros = [(k, v) for k, v in agg.items() if k[0] not in dias]
    if otros:
        otros.sort(key=lambda kv: (kv[0][0],
                                   kv[0][1] or (min(kv[1]["horas"]) if kv[1]["horas"]
                                                else time.max),
                                   orden_ev.get(kv[0][2], 9), kv[0][3],
                                   kv[0][4], kv[0][5]))
        cuerpo = []
        for (dia_k, hora, evidencia, sistema, tipo, _det), g in otros:
            cuerpo.append(_fila_correo(f'{dia_k.strftime("%d/%m")} '
                                      f'{_hora_texto(hora, g)}',
                                      evidencia, sistema, tipo, g))
        partes.append(
            '<h3>Otros días <span class="sub">· correos enviados fuera del rango '
            f'({plural(sum(v["n"] for _, v in otros), "correo")})</span></h3>'
            + _tabla(cols, ["Día · hora"] + head[1:], cuerpo, "correos")
        )
    return (
        '<section class="anexo">'
        '<header class="doc"><h2>B · Correos, día por día</h2>'
        '<div class="sub">Todo lo que el sistema pudo enviar, con la evidencia de '
        'cada fila: <b>Registrado</b> (la base guarda la marca y la hora), '
        '<b>Programado</b> (configuración activa; la base no guarda log de envíos) o '
        '<b>Inferido</b> (deducido de los huecos de lecturas). Las alertas se agrupan '
        'por día y punto (su hora exacta está en el anexo A, <code>--detalle</code>); '
        'el estado actual es <b>un correo por horario</b> con todos sus puntos.'
        '</div></header>'
        + _kpis([
            (fmt_n(tot.get("Registrado", 0)), "Registrados"),
            (fmt_n(tot.get("Programado", 0)), "Programados"),
            (fmt_n(tot.get("Inferido", 0)), "Inferidos"),
            (fmt_n(tot.get("total", 0)), "Total estimado"),
        ], clase="correo")
        + "".join(partes)
        + "</section>"
    )


def _caja_hallazgo(titulo: str, cuerpo: str) -> str:
    return f'<div class="hallazgo"><h4>{esc(titulo)}</h4>{cuerpo}</div>'


def _lista_corta(items, fmt, vacio: str = "Sin datos.") -> str:
    if not items:
        return f'<p class="nota">{vacio}</p>'
    return "<ul>" + "".join(f"<li>{fmt(x)}</li>" for x in items) + "</ul>"


# ---------- HTML: bloques por módulo -----------------------------------------

def _correos_de(modulo: str, correos: Sequence[Correo]) -> list:
    nombre = NOMBRE_MODULO[modulo]
    return [c for c in correos if c.sistema == nombre]


def _alertas_de(modulo: str, eventos: Sequence[Evento],
                limites: Sequence[Limite]) -> list:
    if modulo == "ambiental":
        return list(limites)
    return [e for e in eventos if e.modulo == modulo]


def _criticas_de(modulo: str, alertas: Sequence) -> int:
    if modulo == "ambiental":
        return sum(1 for l in alertas
                   if severidad_ambiental(l.nivel) == "Crítica")
    return sum(1 for e in alertas if e.severidad == "Crítica")


def _episodios_de(modulo: str, episodios: Sequence[Episodio],
                  grupos: Sequence[GrupoLimite]) -> list:
    if modulo == "ambiental":
        return list(grupos)
    return [e for e in episodios if e.modulo == modulo]


def tablon_modulo(empresa: str, modulo: str, dias: Sequence[date],
                  alertas: Sequence, correos: Sequence[Correo]) -> str:
    """Alertas vs correos, día por día, para una empresa de un módulo."""
    head = ["Día", "Alertas", "Críticas", "Correos registrados",
            "Programados", "Inferidos", "Total correos"]
    cols = [18, 12, 11, 17, 15, 13, 14]
    cuerpo = []
    acum = Counter()
    dias_set = set(dias)

    def fila(etiqueta: str, al: list, co: list) -> str:
        reg = sum(c.n for c in co if c.evidencia == "Registrado")
        prog = sum(c.n for c in co if c.evidencia == "Programado")
        inf = sum(c.n for c in co if c.evidencia == "Inferido")
        crit = _criticas_de(modulo, al)
        total = reg + prog + inf
        acum.update({"al": len(al), "cr": crit, "reg": reg, "prog": prog,
                     "inf": inf, "tot": total})
        return (
            f'<tr><td>{etiqueta}</td>'
            f'<td class="num">{fmt_n(len(al))}</td>'
            f'<td class="num">{fmt_n(crit)}</td>'
            f'<td class="num">{fmt_n(reg)}</td>'
            f'<td class="num">{fmt_n(prog)}</td>'
            f'<td class="num">{fmt_n(inf)}</td>'
            f'<td class="num"><b>{fmt_n(total)}</b></td></tr>'
        )

    for dia in dias:
        al = [x for x in alertas
              if x.empresa == empresa and x.cuando.date() == dia]
        co = [c for c in correos if c.empresa == empresa and c.dia == dia]
        cuerpo.append(fila(fmt_fecha(dia), al, co))
    # Un correo puede enviarse fuera del rango (p. ej. la alerta del último día
    # se reporta al siguiente). Se muestra aparte para que los totales cuadren.
    fuera = [c for c in correos
             if c.empresa == empresa and c.dia not in dias_set]
    if fuera:
        cuerpo.append(fila("Otros días (envío fuera del rango)", [], fuera))
    cuerpo.append(
        f'<tr class="total"><td>Total</td>'
        f'<td class="num">{fmt_n(acum["al"])}</td>'
        f'<td class="num">{fmt_n(acum["cr"])}</td>'
        f'<td class="num">{fmt_n(acum["reg"])}</td>'
        f'<td class="num">{fmt_n(acum["prog"])}</td>'
        f'<td class="num">{fmt_n(acum["inf"])}</td>'
        f'<td class="num">{fmt_n(acum["tot"])}</td></tr>'
    )
    return _tabla(cols, head, cuerpo, "tablon")


def _tabla_modulos(eventos: Sequence[Evento], limites: Sequence[Limite],
                   correos: Sequence[Correo]) -> str:
    """Resumen general: alertas vs correos por módulo."""
    head = ["Módulo", "Alertas", "Correos", "Registrados", "Programados",
            "Inferidos"]
    cols = [22, 14, 14, 16, 17, 17]
    cuerpo = []
    total = Counter()
    for modulo in MODULOS:
        al = _alertas_de(modulo, eventos, limites)
        co = _correos_de(modulo, correos)
        reg = sum(c.n for c in co if c.evidencia == "Registrado")
        prog = sum(c.n for c in co if c.evidencia == "Programado")
        inf = sum(c.n for c in co if c.evidencia == "Inferido")
        n = reg + prog + inf
        total.update({"al": len(al), "co": n, "reg": reg, "prog": prog, "inf": inf})
        cuerpo.append(
            f'<tr><td>{esc(NOMBRE_MODULO[modulo])}</td>'
            f'<td class="num">{fmt_n(len(al))}</td>'
            f'<td class="num"><b>{fmt_n(n)}</b></td>'
            f'<td class="num">{fmt_n(reg)}</td>'
            f'<td class="num">{fmt_n(prog)}</td>'
            f'<td class="num">{fmt_n(inf)}</td></tr>'
        )
    cuerpo.append(
        f'<tr class="total"><td>Total</td>'
        f'<td class="num">{fmt_n(total["al"])}</td>'
        f'<td class="num">{fmt_n(total["co"])}</td>'
        f'<td class="num">{fmt_n(total["reg"])}</td>'
        f'<td class="num">{fmt_n(total["prog"])}</td>'
        f'<td class="num">{fmt_n(total["inf"])}</td></tr>'
    )
    return _tabla(cols, head, cuerpo, "modulos")


def detalle_correos_empresa(empresa: str, modulo: str, dias: Sequence[date],
                            correos: Sequence[Correo]) -> str:
    """Correos de una empresa, día por día, con su evidencia (sin repetir el
    tablón: aquí va el detalle, no los totales)."""
    co = [c for c in correos if c.empresa == empresa]
    if not co:
        return '<p class="vacio">Sin correos en el periodo.</p>'
    agg = _agrupar_correos(co)
    orden_ev = {"Registrado": 0, "Programado": 1, "Inferido": 2}
    head = ["Día", "Hora", "Tipo", "Detalle", "N", "Destinatarios", "Nota",
            "Evidencia"]
    cols = [8, 6, 14, 27, 5, 18, 14, 8]
    filas = sorted(
        agg.items(),
        key=lambda kv: (kv[0][0], kv[0][1] or
                        (min(kv[1]["horas"]) if kv[1]["horas"] else time.max),
                        orden_ev.get(kv[0][2], 9), kv[0][4], kv[0][5]),
    )
    cuerpo = []
    for (dia, hora, evidencia, sistema, tipo, _det), g in filas:
        dest = (_dest_celda(tuple(sorted(g["dest"])), True) if g["dest"]
                else '<span class="na">—</span>')
        notas = list(g["notas"])
        det_txt = ", ".join(g["detalle"]) or "—"
        # El estado actual repite los mismos 18 puntos todos los días: se
        # resume aquí y el listado completo queda en la configuración (B).
        m = re.match(r"(\d+) puntos:", det_txt)
        if m:
            det_txt = f"{m.group(1)} puntos (ver configuración)"
        hora_txt = _hora_texto(hora, g)
        if hora_txt == "varias" and len(set(g["horas"])) > 1:
            notas.insert(0, f"{min(g['horas']):%H:%M}–{max(g['horas']):%H:%M}")
        cuerpo.append(
            f'<tr><td>{fmt_fecha(dia)}</td>'
            f'<td class="centro">{esc(hora_txt)}</td>'
            f'<td>{esc(tipo)}</td>'
            f'<td>{esc(det_txt)}</td>'
            f'<td class="num">{fmt_n(g["n"])}</td>'
            f'<td>{dest}</td>'
            f'<td class="nota">{esc(" · ".join(notas)) or "—"}</td>'
            f'<td class="centro">{_evidencia_celda(evidencia)}</td></tr>'
        )
    return _tabla(cols, head, cuerpo, "detalle-correos")


def _kpis_empresa(emp: str, modulo: str, alertas: Sequence,
                  episodios: Sequence, correos: Sequence[Correo],
                  cobertura: Optional[dict], dias: Sequence[date]) -> str:
    al = [x for x in alertas if x.empresa == emp]
    co = [c for c in correos if c.empresa == emp]
    reg = sum(c.n for c in co if c.evidencia == "Registrado")
    prog = sum(c.n for c in co if c.evidencia == "Programado")
    inf = sum(c.n for c in co if c.evidencia == "Inferido")
    crit = _criticas_de(modulo, al)
    pct = (100.0 * crit / len(al)) if al else 0.0
    eps = sum(1 for e in episodios
              if str(getattr(e, "empresa", "")) == emp)
    vals = [(cobertura or {}).get((emp, d), {}).get("pct") for d in dias]
    vals = [v for v in vals if v is not None]
    cob = (sum(vals) / len(vals)) if vals else None
    sub_cob = ("datos de los puntos" if cob is not None
               else ("no aplica" if modulo != "energia" else "sin datos"))
    return _kpis([
        (fmt_n(len(al)), "Alertas", plural(eps, "episodio")),
        (fmt_n(reg + prog + inf), "Correos",
         f"{fmt_n(reg)} registrados · {fmt_n(prog)} programados · "
         f"{fmt_n(inf)} inferidos"),
        (fmt_n(crit), "Críticas", f"{fmt_pct(pct)} de las alertas"),
        (fmt_pct(cob) if cob is not None else "—", "Cobertura media", sub_cob),
    ])


def seccion_modulo(numero: int, modulo: str, d1: date, d2: date,
                   dias: Sequence[date], alertas: Sequence,
                   episodios: Sequence, correos: Sequence[Correo],
                   cobertura: Optional[dict] = None) -> str:
    """2/3/4. Un módulo: KPIs por empresa y su detalle de correos."""
    nombre = NOMBRE_MODULO[modulo]
    cmod = _correos_de(modulo, correos)
    empresas = sorted({x.empresa for x in alertas}
                      | {c.empresa for c in cmod
                         if c.empresa and c.empresa != "(global)"})
    bloques = []
    cob_mod = cobertura if modulo == "energia" else None
    for emp in empresas:
        bloques.append(
            f"<h3>{esc(emp)}</h3>"
            + _kpis_empresa(emp, modulo, alertas, episodios, cmod, cob_mod, dias)
            + '<h4>Correos, día por día</h4>'
            + detalle_correos_empresa(emp, modulo, dias, cmod)
        )
    glob = [c for c in cmod if c.empresa == "(global)"]
    if glob:
        bloques.append(
            '<h3>Resumen diario de alertas <span class="sub">· global</span></h3>'
            + _kpis_empresa("(global)", modulo, [], [], cmod, cob_mod, dias)
            + '<h4>Correos, día por día</h4>'
            + detalle_correos_empresa("(global)", modulo, dias, cmod)
        )
    if not bloques:
        bloques.append('<p class="vacio">Sin alertas ni correos en el periodo.</p>')
    return (
        '<section class="modulo">'
        '<header class="doc">'
        f"<h2>{numero}. {nombre}</h2>"
        f'<div class="sub">{esc(rango_largo(d1, d2))} · '
        f'{plural(len(dias), "día")} · '
        f'{plural(len(alertas), "alerta")}</div></header>'
        + "".join(bloques)
        + "</section>"
    )


def seccion_resumen(d1: date, d2: date, tot: dict, eventos: Sequence[Evento],
                    limites: Sequence[Limite], correos: Sequence[Correo],
                    config: Optional[dict], hall: dict, generado: datetime,
                    corte: int, empresas: Sequence[str]) -> str:
    """1. Resumen general: alertas vs correos por módulo."""
    filtro = (" · Filtro: " + ", ".join(empresas)) if empresas else ""
    dias = [d1 + timedelta(days=i) for i in range((d2 - d1).days + 1)]
    tot_correos = totales_correos(correos)
    sin_datos = (config or {}).get("nodata") or []
    activos = [d for d in sin_datos if d.get("activo")]
    con_aviso = [d for d in sin_datos if d.get("ultimo_aviso")]
    ultimo = (config or {}).get("ultimo_resumen")

    kpis = _kpis([
        (fmt_n(tot_correos.get("total", 0)), "Correos del periodo",
         f'{fmt_n(tot_correos.get("Registrado", 0))} registrados · '
         f'{fmt_n(tot_correos.get("Programado", 0))} programados · '
         f'{fmt_n(tot_correos.get("Inferido", 0))} inferidos'),
        (fmt_n(tot["alertas"]), "Alertas registradas",
         f'{plural(tot["episodios"], "episodio")} agrupados'),
        (fmt_n(tot["criticas"]), "Críticas",
         f'{plural(tot["huecos"], "hueco")} sin datos'),
        (fmt_n(tot["puntos"]), "Puntos / equipos",
         f'{plural(tot["empresas"], "empresa", "empresas")} · '
         f'{plural(tot["sedes"], "sede")}'),
    ])
    guia = (
        '<div class="nota-caja">'
        "<b>Cómo se lee.</b> Cada alerta genera un correo. Además, la plataforma "
        "envía reportes programados: <b>estado actual</b> 2 veces al día (un correo "
        "con los 18 puntos), <b>cobertura diaria</b> y <b>consumo mensual</b>. En "
        "ambiental, solo <b>humedad y temperatura</b> de Sanna generan correo "
        "(5 destinatarios); CO₂ no. Los avisos de «sin datos» de ambiental se "
        "deducen de los huecos de lecturas. "
        "En la tabla, <b>Registrado</b> = la base guarda la hora del envío; "
        "<b>Programado</b> = solo hay configuración (no hay log de envíos); "
        "<b>Inferido</b> = deducido de otro dato."
        "</div>"
    )
    bullets_resumen = [
        f'<li>Resumen diario: '
        + (f'último el <b>{fmt_fecha_hora(ultimo)}</b>' if ultimo
           else 'sin registros')
        + '.</li>'
    ]
    if activos:
        bullets_resumen.append(
            f'<li>Sin datos de energía: <b>{fmt_n(len(activos))}</b> monitores '
            f'activos y <b>{fmt_n(len(con_aviso))}</b> con envío registrado.</li>'
        )
    cajas = (
        _caja_hallazgo("Dónde se concentran las alertas",
                       _lista_corta(hall["top_puntos"][:3],
                                    lambda x: f'{esc(x[0])} — {fmt_n(x[1])}'))
        + _caja_hallazgo("Correos que dejaron de salir",
                         "<ul>" + "".join(bullets_resumen) + "</ul>")
        + _caja_hallazgo("Alertas sin correo configurado",
                         f'<p class="nota">{fmt_n(hall["n_sin_correo"])} alertas de '
                         "puntos cuyo umbral tiene el correo desactivado o sin "
                         "destinatarios.</p>"
                         + _lista_corta(hall["sin_correo"][:2],
                                        lambda x: f'{esc(x[0])} — {fmt_n(x[1])}'))
    )
    return (
        '<section class="portada">'
        '<header class="doc">'
        "<h1>Alertas y correos del periodo</h1>"
        f'<div class="sub">Energía · Agua · Ambiental — {esc(rango_largo(d1, d2))}</div>'
        f'<div class="meta"><span><b>Generado:</b> {fmt_fecha_hora(generado)} (Lima)</span>'
        f'<span><b>Rango:</b> {d1.isoformat()} → {d2.isoformat()} '
        f'({len(dias)} {"día" if len(dias) == 1 else "días"})</span>'
        f'<span><b>Corte de episodio:</b> {corte} min</span>'
        f"<span>{esc(filtro)}</span></div>"
        '<div class="indice"><b>Contenido:</b> 1. Resumen · 2. Energía · 3. Agua · '
        '4. Ambiental</div>'
        "</header>"
        + kpis + guia
        + '<div class="panel-2">'
        + chart_correos_dia(dias, correos)
        + "<div><h3>Alertas vs correos por módulo</h3>"
        + _tabla_modulos(eventos, limites, correos) + "</div>"
        + "</div>"
        + "<h3>Lo que conviene mirar</h3>"
        + f'<div class="hallazgos tres">{cajas}</div>'
        + "</section>"
    )


def bloque_tablas(eventos: Sequence[Evento], episodios: Sequence[Episodio],
                  limites: Sequence[Limite], grupos: Sequence[GrupoLimite],
                  huecos: Sequence[Hueco], dias: Sequence[date]) -> str:
    """Las cuatro tablas de alertas de un periodo."""
    ep_e = [e for e in episodios if e.modulo == "energia"]
    ep_a = [e for e in episodios if e.modulo == "agua"]
    return (
        _tabla_episodios(ep_e, "Energía — episodios",
                         f"{plural(len(ep_e), 'episodio')} · "
                         f"{plural(sum(e.n for e in ep_e), 'alerta')}")
        + _tabla_episodios(ep_a, "Agua — episodios",
                           f"{plural(len(ep_a), 'episodio')} · "
                           f"{plural(sum(e.n for e in ep_a), 'alerta')}")
        + _tabla_limites(grupos, dias)
        + _tabla_huecos(huecos)
    )


def seccion_alertas(d1: date, d2: date, tot: dict, filas: Sequence[dict],
                    dias: Sequence[date], conteos: dict, tipos: Sequence[tuple],
                    comparativa: Sequence[dict], hall: dict, generado: datetime,
                    corte: int, empresas: Sequence[str], detalle: str = "") -> str:
    """3. Alertas: panorama del periodo y, si es una sola semana, su detalle."""
    filtro = (" · Filtro: " + ", ".join(empresas)) if empresas else ""
    kpis = _kpis([
        (fmt_n(tot["alertas"]), "Alertas registradas",
         f'{plural(tot["episodios"], "episodio")} agrupados'),
        (fmt_n(tot["criticas"]), "Críticas", f'de {fmt_n(tot["alertas"])}'),
        (fmt_n(tot["puntos"]), "Puntos / equipos",
         f'{plural(tot["destinatarios"], "destinatario")}'),
        (fmt_n(tot["huecos"]), "Huecos sin datos", "ambiental · inferidos"),
    ])
    return (
        '<section class="alertas">'
        '<header class="doc">'
        "<h2>3. Alertas del periodo</h2>"
        f'<div class="sub">{esc(rango_largo(d1, d2))} · '
        f'{plural(len(dias), "día")} · {fmt_n(tot["empresas"])} empresas'
        f"{esc(filtro)}</div></header>"
        + kpis
        + '<div class="charts dos">'
        + chart_dias(dias, conteos) + chart_tipos(tipos)
        + "</div>"
        + f"<h3>Resumen por empresa <span class=\"sub\">· {plural(len(filas), 'empresa', 'empresas')} con alertas</span></h3>"
        + _tabla_resumen(filas)
        + "<h3>Comparación con periodos anteriores</h3>"
        + _tabla_comparativa(comparativa)
        + f'<p class="nota">{fmt_n(hall["fuera_horario"])} de '
        f'{fmt_n(hall["total"])} registros ({fmt_pct(hall["pct_fuera"])}) '
        "ocurrieron fuera del horario 08:00–20:00.</p>"
        + detalle
        + "</section>"
    )


def seccion_periodo(d1: date, d2: date, dias: Sequence[date], numero: int,
                    total_sem: int, alertas: int, episodios: int,
                    tablas: str) -> str:
    """Semana X: solo las tablas de detalle (el panorama ya está en 3)."""
    return (
        '<section class="semana">'
        '<header class="doc">'
        f"<h2>Semana {numero} de {total_sem} · {esc(rango_largo(d1, d2))}</h2>"
        f'<div class="sub">{plural(len(dias), "día")} · '
        f'{plural(alertas, "alerta")} · {plural(episodios, "episodio")}</div>'
        "</header>"
        + tablas
        + "</section>"
    )


# ---------- HTML: anexos ------------------------------------------------------

DETALLE_MAX = 4000


def anexo_detalle(eventos: Sequence[Evento], tope: int = DETALLE_MAX) -> str:
    eventos = sorted(eventos, key=lambda e: (e.cuando, ORDEN_MODULO.get(e.modulo, 9)))
    n = len(eventos)
    recortados = n > tope
    eventos = eventos[:tope]
    if not eventos:
        cuerpo = []
    else:
        cuerpo = []
        for e in eventos:
            cuerpo.append(
                f'<tr><td>{fmt_fecha(e.cuando.date())}</td>'
                f'<td>{fmt_hora(e.cuando)}</td>'
                f'<td>{NOMBRE_MODULO.get(e.modulo, e.modulo)}</td>'
                f'<td>{esc(e.empresa)}</td>'
                f'<td>{esc(e.sede)}</td>'
                f'<td>{esc(e.grupo)}</td>'
                f'<td>{esc(e.punto)}</td>'
                f'<td>{esc(e.tipo)}</td>'
                f'<td>{esc(e.subtipo)}</td>'
                f'<td class="centro">{_badge(e.severidad)}</td>'
                f'<td class="num">{fmt_valor(e.valor, e.valor, e.unidad)}</td>'
                f'<td class="centro">{_correo_celda(e.reportada)}</td>'
                f'<td class="centro">{_resumen_celda(int(bool(e.resumen)), 1)}</td>'
                f'<td>{_dest_celda(e.destinatarios, e.correo_activo)}</td></tr>'
            )
    head = ["Fecha", "Hora", "Mód.", "Empresa", "Sede", "Tablero / tubería",
            "Punto / medidor", "Tipo", "Subtipo", "Sev.", "Valor", "Notif.",
            "Resumen", "Destinatarios"]
    cols = [6, 4, 5, 12, 7, 10, 12, 11, 8, 4, 5, 4, 4, 13]
    aviso = ""
    if recortados:
        aviso = (f'<div class="nota-caja">Se muestran los primeros {fmt_n(tope)} de '
                 f'{fmt_n(n)} registros. Reduce el rango o usa --corte para ver el resto '
                 "en los episodios.</div>")
    return (
        '<section class="anexo">'
        '<header class="doc"><h2>Anexo A · Evento por evento</h2>'
        f'<div class="sub">Energía y agua: una fila por alerta registrada '
        f"({fmt_n(n)} en el periodo). No incluye Limit Alerts de ambiental, que se "
        "agrupan en el anexo de cada semana.</div></header>"
        + aviso
        + _tabla(cols, head, cuerpo, "detalle")
        + "</section>"
    )


def anexo_config(config: dict) -> str:
    umbrales = "".join(
        f'<tr><td>{esc(c.empresa)}</td><td>{esc(c.tipos)}</td>'
        f'<td class="num">{fmt_n(c.n)}</td>'
        f'<td class="centro">{_correo_celda(c.correo)}</td>'
        f'<td>{_dest_celda(c.destinatarios, c.correo)}</td></tr>'
        for c in config["umbrales"]
    ) or '<tr><td colspan="5" class="vacio">Sin umbrales configurados.</td></tr>'

    programados = "".join(
        f'<tr><td>{esc(c.empresa)}</td><td>{esc(c.tipo)}</td>'
        f'<td>{esc(c.cuando)}</td>'
        f'<td class="centro">{_correo_celda(c.activo)}</td>'
        f'<td class="num">{fmt_n(c.n)}</td>'
        f'<td>{_dest_celda(c.destinatarios, c.activo)}</td></tr>'
        for c in config["programados"]
    ) or '<tr><td colspan="6" class="vacio">Sin correos programados.</td></tr>'

    def fila_nodata(d: dict, extra: str) -> str:
        estado = d["estado"] or "—"
        caido = fmt_fecha_hora(d["caido_desde"]) if d["caido_desde"] else "—"
        aviso = fmt_fecha_hora(d["ultimo_aviso"]) if d["ultimo_aviso"] else "—"
        if d["abierto"]:
            estado += " (abierto)"
        return (
            f"<tr><td>{esc(d['empresa'])}</td><td>{esc(d['sede'])}</td>"
            f"<td>{esc(extra)}</td>"
            f'<td class="num">{d["timeout"]} min</td>'
            f'<td class="num">{d["recordatorio"] or "—"}</td>'
            f'<td class="centro">{_correo_celda(bool(d["correo"]))}</td>'
            f"<td>{_dest_celda(d['correo'], bool(d['correo']))}</td>"
            f"<td>{esc(estado)}</td><td>{caido}</td><td>{aviso}</td></tr>"
        )

    nodata_energia = "".join(
        fila_nodata(d, d["punto"]) for d in config["nodata"]
    ) or '<tr><td colspan="10" class="vacio">Sin monitores de sin datos de energía.</td></tr>'
    nodata_amb = "".join(
        fila_nodata(d, f'{d["sala"]} · {d["equipo"]}') for d in config["ambiental"]
    ) or '<tr><td colspan="10" class="vacio">Sin equipos ambientales con aviso de sin datos.</td></tr>'

    return (
        '<section class="anexo">'
        '<header class="doc"><h2>B · Configuración de correos (estado actual)</h2>'
        '<div class="sub">La base no versiona los cambios de destinatarios ni guarda '
        "un log de envíos: esta es la configuración vigente al generar el PDF.</div>"
        "</header>"
        "<h3>Umbrales de alerta</h3>"
        + _tabla([26, 34, 6, 7, 27],
                 ["Empresa", "Tipos de alerta", "N°", "Correo", "Destinatarios"],
                 [umbrales], "config")
        + "<h3>Correos programados (resúmenes)</h3>"
        + _tabla([20, 18, 18, 7, 5, 32],
                 ["Empresa", "Tipo", "Cuándo", "Activo", "N°", "Destinatarios"],
                 [programados], "config")
        + "<h3>Sin datos — energía</h3>"
        + '<p class="nota">Aviso cuando un punto deja de reportar durante el tiempo de '
        "espera indicado. El estado y la última fecha de aviso son los actuales.</p>"
        + _tabla([12, 9, 20, 7, 7, 6, 15, 9, 8, 8],
                 ["Empresa", "Sede", "Punto", "Espera", "Record.", "Correo",
                  "Destinatarios", "Estado", "Caído desde", "Último aviso"],
                 [nodata_energia], "config")
        + "<h3>Sin datos — ambiental</h3>"
        + '<p class="nota">Los correos de sin datos ambiental no tienen histórico: los '
        "huecos de cada semana se reconstruyen con esta espera y se marcan como inferidos.</p>"
        + _tabla([10, 8, 20, 7, 7, 6, 14, 9, 9, 9],
                 ["Empresa", "Sede", "Sala · equipo", "Espera", "Record.", "Correo",
                  "Destinatarios", "Estado", "Caído desde", "Último aviso"],
                 [nodata_amb], "config")
        + "<h3>Correos de alertas — ambiental</h3>"
        + '<p class="nota">Regla confirmada por el negocio: solo <b>humedad y '
        "temperatura</b> generan correo, y solo en Sanna; <b>CO₂ no</b> genera. "
        "La base no guarda el log de estos envíos ni sus destinatarios.</p>"
        + _tabla([14, 26, 8, 52],
                 ["Empresa", "Indicadores", "Correo", "Destinatarios"],
                 [f'<tr><td>Sanna</td><td>Humedad, Temperatura</td>'
                  f'<td class="centro">{_correo_celda(True)}</td>'
                  f'<td>{_dest_celda(DEST_AMBIENTAL, True)}</td></tr>'
                  f'<tr><td>Sanna</td><td>CO₂</td>'
                  f'<td class="centro">{_correo_celda(False)}</td>'
                  f'<td><span class="na">No aplica</span></td></tr>'],
                 "config")
        + '<p class="nota">Último resumen diario de alertas registrado: '
        + (f'<b>{fmt_fecha_hora(config["ultimo_resumen"])}</b>. '
           if config.get("ultimo_resumen") else 'sin registros. ')
        + "Los resúmenes posteriores a esa fecha no aparecen marcados en la base.</p>"
        + "</section>"
    )


def anexo_metodologia(d1: date, d2: date, corte: int, generado: datetime) -> str:
    return (
        '<section class="anexo">'
        '<header class="doc"><h2>C · Metodología y limitaciones</h2>'
        f'<div class="sub">Rango {esc(rango_largo(d1, d2))} · corte {corte} min · '
        f'generado {fmt_fecha_hora(generado)}</div></header>'
        '<div class="hallazgos">'
        '<div class="hallazgo"><h4>Fuentes de datos</h4><ul>'
        "<li><b>Energía:</b> <i>alerts_alert</i> unido a <i>alerts_alertthreshold</i> "
        "(tipo, correo activo y destinatarios).</li>"
        "<li><b>Agua:</b> <i>alerts_wateralert</i> con el mismo umbral.</li>"
        "<li><b>Ambiental:</b> <i>alerts_limitalert</i> («Limit Alerts»: CO₂, "
        "temperatura y humedad por sala). No generan correo por sí mismas.</li>"
        "<li><b>Sin datos ambiental:</b> huecos de <i>readings_reading</i> ≥ "
        "<i>timeout_minutes</i> del equipo.</li>"
        "<li><b>Correos de reportes:</b> <i>alerts_currentstatusreportschedule</i> "
        "(estado actual), <i>alerts_dailycoveragereportschedule</i> (cobertura "
        "diaria) y <i>alerts_monthlyconsumptionreportschedule</i> (consumo mensual). "
        "Solo guardan la configuración: no hay log de envíos.</li>"
        "<li><b>Correos de alertas ambientales:</b> regla de negocio confirmada "
        "(humedad y temperatura en Sanna; CO₂ no). La base no guarda el envío ni "
        "los destinatarios.</li>"
        "<li><b>Resumen diario:</b> <i>alerts_alert.email_summary_reported_at</i>.</li>"
        "<li>Fechas y horas en <b>America/Lima</b> (UTC−5), sin horario de verano.</li>"
        "</ul></div>"
        '<div class="hallazgo"><h4>Qué significa cada columna</h4><ul>'
        "<li><b>N</b> — alertas agrupadas en el episodio.</li>"
        "<li><b>Notif.</b> — la plataforma marcó la alerta como reportada "
        "(<i>reported_at</i>). <b>No</b> prueba que se enviara un correo.</li>"
        "<li><b>Resumen</b> — incluida en un correo resumen "
        "(<i>email_summary_reported_at</i>; históricamente a las 17:00).</li>"
        "<li><b>Correo</b> — el umbral tiene correo activo y destinatarios "
        "configurados hoy.</li>"
        "<li><b>Resueltas</b> (ambiental) — la alerta tiene <i>resolved</i> "
        "verdadero; no implica que alguien la atendiera.</li>"
        "</ul></div>"
        '<div class="hallazgo"><h4>Limitaciones conocidas</h4><ul>'
        "<li>Los destinatarios son la configuración <b>actual</b>: si cambiaron "
        "durante el periodo, el PDF no lo refleja.</li>"
        "<li>No existe un log histórico de correos enviados. «Notif.» es lo más "
        "cercano en energía y agua; en ambiental los avisos de sin datos son "
        "<b>inferidos</b>.</li>"
        "<li><b>Programado ≠ enviado:</b> los reportes de estado actual, cobertura "
        "y consumo solo acreditan que la configuración estaba activa a esa hora. Si "
        "el proceso falló, el PDF no lo detecta.</li>"
        "<li>Las alertas ambientales de humedad/temperatura se cuentan como <b>1 "
        "correo por alerta</b> (regla de negocio); si la plataforma las agrupa, el "
        "número real sería menor.</li>"
        "<li>Los correos condicionales (sin datos) no se cuentan como programados: "
        "solo salen si el equipo se cae.</li>"
        "<li><i>alerts_incidentalert</i> (incidentes ambiental) está vacía en "
        "producción y no se usa.</li>"
        "<li>Las alertas de agua son recientes y escasas (medidores instalados "
        "entre sep y oct de 2026).</li>"
        "<li>Episodios con corte mayor agrupan más; con 0 cada alerta es su "
        "propio episodio.</li>"
        "<li>Se excluyen las empresas de prueba, igual que el monitor en vivo y "
        "el reporte de conectividad.</li>"
        "</ul></div>"
        '<div class="hallazgo"><h4>Reproducir el reporte</h4><ul>'
        '<li><code>python scripts/reporte_alertas.py --desde '
        f"{d1.isoformat()} --hasta {d2.isoformat()}</code></li>"
        "<li><code>--detalle</code> agrega el anexo evento por evento.</li>"
        "<li><code>--empresa Sanna</code> filtra una o varias empresas.</li>"
        "<li><code>--html</code> guarda también el HTML y no necesita Chrome.</li>"
        "<li>El script solo lee: las sesiones son <i>read-only</i> y con "
        "<i>statement_timeout</i> de 120 s.</li>"
        "</ul></div>"
        "</div></section>"
    )


# ---------- Ensamblado --------------------------------------------------------

def armar_html(d1: date, d2: date, eventos: Sequence[Evento],
               limites: Sequence[Limite], huecos: Sequence[Hueco], config: dict,
               comparativas: Sequence[dict], generado: datetime, corte: int,
               empresas: Optional[Sequence[str]] = None,
               detalle: bool = False,
               correos: Sequence[Correo] = (),
               cobertura: Optional[dict] = None) -> str:
    if empresas:
        eventos = filtrar_empresas(eventos, empresas)
        limites = filtrar_empresas(limites, empresas)
        huecos = filtrar_empresas(huecos, empresas)
        correos = filtrar_empresas(correos, empresas)
        config = _filtrar_config(config, empresas)
    dias = [d1 + timedelta(days=i) for i in range((d2 - d1).days + 1)]
    episodios = agrupar_episodios(eventos, corte)
    grupos = agrupar_limites(limites, dias)
    tot = totales(eventos, limites, episodios, grupos, huecos)
    filas = resumen_empresas(eventos, limites, episodios, grupos)
    hall = hallazgos(eventos, limites, grupos)

    partes = [
        seccion_resumen(d1, d2, tot, eventos, limites, correos, config, hall,
                        generado, corte, empresas or []),
    ]
    for numero, modulo in enumerate(MODULOS, 2):
        partes.append(seccion_modulo(
            numero, modulo, d1, d2, dias,
            _alertas_de(modulo, eventos, limites),
            _episodios_de(modulo, episodios, grupos),
            correos, cobertura,
        ))
    return (
        "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        "<title>Alertas y correos del periodo</title>"
        f"<style>{CSS}</style></head><body>"
        + "".join(partes)
        + "</body></html>"
    )


# ---------- main --------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Reporte de alertas y notificaciones por correo (PDF).",
        epilog="Este reporte es manual: cambia --desde/--hasta para elegir el periodo.",
    )
    ap.add_argument("--desde", help="AAAA-MM-DD (default: lunes de la semana pasada)")
    ap.add_argument("--hasta", help="AAAA-MM-DD (default: domingo de la semana pasada)")
    ap.add_argument("--empresa", action="append",
                    help="solo esta empresa; repetible (default: todas)")
    ap.add_argument("--corte", type=int, default=CORTE_MIN,
                    help=f"minutos para agrupar alertas en un episodio (default {CORTE_MIN}; "
                         "0 = una alerta por episodio)")
    ap.add_argument("--detalle", action="store_true",
                    help="agregar anexo A con una fila por alerta (máx. 4,000)")
    ap.add_argument("--sin-huecos", action="store_true",
                    help="no reconstruir los correos de sin datos ambiental (más rápido)")
    ap.add_argument("--sin-correos", action="store_true",
                    help="no calcular ni listar los correos del periodo (más rápido)")
    ap.add_argument("--out", help="ruta del PDF (default: ~/Downloads/reporte_alertas_...)")
    ap.add_argument("--html", action="store_true", help="guardar también el HTML junto al PDF")
    ap.add_argument("--chrome", help="ruta a Chrome/Edge (si no se detecta solo)")
    args = ap.parse_args(argv)

    hoy = datetime.now(LIMA).date()
    if bool(args.desde) != bool(args.hasta):
        ap.error("usa --desde y --hasta juntos")
    d1, d2 = ((date.fromisoformat(args.desde), date.fromisoformat(args.hasta))
              if args.desde else semana_pasada(hoy))
    if d2 < d1:
        ap.error("--hasta no puede ser anterior a --desde")
    if (d2 - d1).days + 1 > DIAS_MAX:
        ap.error(f"el rango no puede superar {DIAS_MAX} días; divídelo en varios reportes")
    if args.corte < 0:
        ap.error("--corte no puede ser negativo")

    print(f"Reporte de alertas: {d1} → {d2} (corte {args.corte} min)")
    generado = datetime.now(LIMA)
    try:
        eventos = cargar_energia(inicio_dia(d1), fin_dia(d2))
        print(f"  energía:   {fmt_n(len(eventos))} alertas")
        agua = cargar_agua(inicio_dia(d1), fin_dia(d2))
        eventos += agua
        print(f"  agua:      {fmt_n(len(agua))} alertas")
        limites = cargar_ambiental(inicio_dia(d1), fin_dia(d2))
        print(f"  ambiental: {fmt_n(len(limites))} Limit Alerts")
        huecos = [] if args.sin_huecos else cargar_huecos(d1, d2)
        print(f"  sin datos: {fmt_n(len(huecos))} huecos inferidos"
              + (" (omitido con --sin-huecos)" if args.sin_huecos else ""))
        config = cargar_config()
        cobertura = cargar_cobertura(d1 - timedelta(days=1), d2)
        print(f"  cobertura: {fmt_n(len(cobertura))} días-empresa con datos")
        eventos = filtrar_empresas(eventos, args.empresa)
        limites = filtrar_empresas(limites, args.empresa)
        huecos = filtrar_empresas(huecos, args.empresa)
        if args.empresa:
            print("  filtro:    " + ", ".join(args.empresa))
        correos = ([] if args.sin_correos
                   else cargar_correos(d1, d2, eventos, limites, huecos, config,
                                       cobertura))
        correos = filtrar_empresas(correos, args.empresa)
        tot_correos = totales_correos(correos)
        print(f"  correos:   {fmt_n(tot_correos.get('total', 0))} "
              f"({fmt_n(tot_correos.get('Registrado', 0))} registrados, "
              f"{fmt_n(tot_correos.get('Programado', 0))} programados, "
              f"{fmt_n(tot_correos.get('Inferido', 0))} inferidos)"
              + (" (omitido con --sin-correos)" if args.sin_correos else ""))
    finally:
        tunnel.close_tunnel()  # solo cierra los túneles que abrió este proceso

    if not eventos and not limites and not correos:
        raise SystemExit(f"No hay alertas ni correos entre {d1} y {d2}"
                         + (f" para {', '.join(args.empresa)}" if args.empresa else "") + ".")

    html_txt = armar_html(d1, d2, eventos, limites, huecos, config, [],
                          generado, args.corte, args.empresa, args.detalle,
                          correos, cobertura)

    sufijo = "".join(f"_{_slug(e)}" for e in (args.empresa or []))
    nombre = f"reporte_alertas{sufijo}_{d1}_a_{d2}.pdf"
    descargas = Path.home() / "Downloads"
    destino = (Path(args.out).expanduser() if args.out
               else (descargas if descargas.is_dir() else Path.cwd()) / nombre)
    if args.html:
        destino.with_suffix(".html").write_text(html_txt, encoding="utf-8")
        print(f"HTML: {destino.with_suffix('.html')}")
    try:
        chrome = buscar_chrome(args.chrome)
    except SystemExit as exc:
        if not args.html:
            raise
        print(f"Sin Chrome/Edge no se genera el PDF ({exc})")
        return 0
    imprimir_pdf(html_txt, destino, chrome)

    episodios = agrupar_episodios(eventos, args.corte)
    grupos = agrupar_limites(limites, [d1 + timedelta(days=i)
                                       for i in range((d2 - d1).days + 1)])
    print(f"PDF: {destino}")
    print(f"  {fmt_n(len(eventos) + len(limites))} alertas · "
          f"{fmt_n(len(episodios) + len(grupos))} episodios/grupos · "
          f"{fmt_n(sum(1 for e in eventos if e.severidad == 'Crítica') + sum(1 for l in limites if severidad_ambiental(l.nivel) == 'Crítica'))} críticas")
    print(f"  correos: {fmt_n(tot_correos.get('Registrado', 0))} registrados · "
          f"{fmt_n(tot_correos.get('Programado', 0))} programados · "
          f"{fmt_n(tot_correos.get('Inferido', 0))} inferidos · "
          f"{fmt_n(tot_correos.get('total', 0))} en total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
