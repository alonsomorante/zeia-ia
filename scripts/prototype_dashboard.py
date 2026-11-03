#!/usr/bin/env python3
"""PROTOTIPO DESECHABLE: maqueta del dashboard del cliente con datos reales.

Pregunta que responde: ¿cómo debería verse el dashboard que usará el cliente?
Tres variantes estructuralmente distintas en un solo HTML (se cambian con la
barra flotante o con ?variant=A|B|C):
  A · Tablero    KPIs + grilla de widgets editables + chat lateral con 📌
  B · Informe    informe vivo al estilo del reporte mensual, con sugerencias
  C · Explorador árbol sede → tablero → circuito + lienzo + barra de preguntas

Datos: energía REAL de Sanna San Borja y Oechsle Salaverry (producción, solo
lectura: src/db.validate_sql + sesión read-only), agregada por punto y hora
desde el día 1 de hace dos meses. El chat de la maqueta responde preguntas de
ejemplo calculadas con esos datos (en el producto las respondería la IA).

Uso:
  python scripts/prototype_dashboard.py            # consulta (~1-3 min), genera y abre
  python scripts/prototype_dashboard.py --reusar   # no consulta: usa el último JSON
  python scripts/prototype_dashboard.py --no-abrir

Salida (gitignored porque trae datos de clientes):
  analisis/prototype_dashboard.html        abrir en el navegador (autocontenido)
  analisis/prototype_dashboard_datos.json  datos bajados (para --reusar)
Plantilla: web/static/prototype_dashboard.html
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.avisos_datos import consultar_db  # noqa: E402  (solo lectura)

LIMA = timezone(timedelta(hours=-5))          # Perú no tiene horario de verano
PLANTILLA = ROOT / "web" / "static" / "prototype_dashboard.html"
ECHARTS = ROOT / "web" / "static" / "vendor" / "echarts.min.js"
HTML2CANVAS = ROOT / "web" / "static" / "vendor" / "html2canvas.min.js"   # captura del dashboard
SALIDA = ROOT / "analisis" / "prototype_dashboard.html"
CACHE = ROOT / "analisis" / "prototype_dashboard_datos.json"
TRAMO_DIAS = 7                                 # consultas por tramos (carga suave)
SALTO_X = 25                                   # un paso > 25× el p99 del punto = salto de contador
MARCA_ECHARTS = '<script src="vendor/echarts.min.js"></script>'
MARCA_H2C = '<script src="vendor/html2canvas.min.js"></script>'
MARCA_DATOS = '<script id="datos" type="application/json">null</script>'

SQL_HORAS = """
    SELECT measurement_point_id AS uid,
           date_trunc('hour', created_at AT TIME ZONE 'America/Lima') AS hora,
           max("EPpos_value") AS ep, count(*) AS n
    FROM readings_reading
    WHERE measurement_point_id = ANY(:ids) AND created_at >= :ini AND created_at < :fin
      AND "EPpos_value" > 0
    GROUP BY 1, 2"""
SQL_ULTIMA = """
    SELECT max(created_at) AS ult FROM readings_reading
    WHERE measurement_point_id = ANY(:ids) AND created_at >= :desde"""
SQL_DIA = """
    SELECT max("EPpos_value") - min("EPpos_value") AS kwh FROM readings_reading
    WHERE measurement_point_id = :uid AND created_at >= :ini AND created_at < :fin
      AND "EPpos_value" > 0"""
SQL_TARIFA = """
    SELECT b.currency AS moneda, b.energy_unit AS unidad, b.charge_for_active_energy AS precio,
           p.power_contracted AS contratada
    FROM enterprises_energyheadquarter eh
    LEFT JOIN enterprises_billingdata b ON b.id = eh.billing_data_id
    LEFT JOIN enterprises_power p ON p.energy_headquarter_id = eh.id
    WHERE eh.id = :sede"""


def nodo(nid: str, nombre: str, punto, detalle: str = "", hijos: list | None = None) -> dict:
    return {"id": nid, "nombre": nombre, "punto": punto, "detalle": detalle,
            "hijos": hijos or []}


def hoja(punto: int, nombre: str, detalle: str = "") -> dict:
    return nodo(f"p{punto}", nombre, punto, detalle)


# Árbol de medición de cada sede. La base no guarda qué tablero cuelga de cuál:
# - Sanna San Borja: la Llave general del TG-P1 (punto 1) ES el total de la sede.
#   Validado: junio-2026 = 146,363 kWh = los 146.36 MWh del reporte mensual del
#   cliente. TGA (4), TG-RT (2, Tomógrafo) y el cuarto de bombas (3) cuelgan de
#   ella; la diferencia son "otros no desagregados". Sumar las 4 llaves duplica.
# - Oechsle Salaverry: total = TG-TR1 (75) + TG-TR2 (76), criterio del cliente
#   (son los dos tableros con is_main). TTA-TR1 (67/68) y TGE-TR1 (69/70) se
#   muestran aparte, fuera del total.
CLIENTES = {
    "sanna": {
        "empresa": "Sanna", "sede": "San Borja", "sede_id": 1, "rubro": "Clínica",
        "tarifa_ref": {"moneda": "S/", "kwh": 0.155,
                       "nota": "Con 0.155 S/ por kWh, la tarifa implícita del reporte mensual de "
                               "junio: Sanna no tiene tarifa registrada (por validar)."},
        "metodo": "El total de la sede es la llave general del TG-P1: en junio marcó 146.36 MWh, "
                  "lo mismo que el reporte mensual. TGA, TG-RT y el cuarto de bombas cuelgan de "
                  "ella; la diferencia es «otros no desagregados».",
        "arbol": nodo("sede", "Sanna San Borja", 1, "Llave general del TG-P1", [
            nodo("t4", "TGA (N)", 4, "Llave general TGA", [
                hoja(40, "Data center UPS / Transformadores"),
                hoja(34, "TN-P-12 / TE-P-12"),
                hoja(5, "Resonador"),
                hoja(37, "Hemodiálisis"),
                hoja(36, "Sala de operaciones · piso 4"),
                hoja(35, "RX-PB"),
                hoja(39, "Aire acondicionado cocina"),
                hoja(41, "Pediatría A/AE"),
                hoja(38, "Ascensor lado derecho"),
            ]),
            nodo("t2", "TG-RT · Tomógrafo", 2, "Tablero General TG-RT"),
            nodo("t3", "Cuarto de bombas", 3, "Fuerza y control de bombas de agua"),
        ]),
        "fuera": [],
    },
    "oechsle": {
        "empresa": "Oechsle", "sede": "Salaverry", "sede_id": 67, "rubro": "Tienda por departamentos",
        "tarifa_ref": None,
        "metodo": "El total de la tienda es TG-TR1 + TG-TR2, el criterio acordado con el cliente. "
                  "TTA-TR1 y TGE-TR1 se muestran aparte y no suman al total.",
        "arbol": nodo("sede", "Oechsle Salaverry", None, "TG-TR1 + TG-TR2 (criterio del cliente)", [
            nodo("t39", "TG-TR2 · HVAC", 76, "TG-TR2 (TF-AA)", [
                hoja(77, "Chiller 1"), hoja(78, "Chiller 2"),
                hoja(79, "TF-BAHP"), hoja(80, "TF-BAHS"),
                hoja(81, "TF-UMAS 1"), hoja(82, "TF-UMAS 2"),
                hoja(83, "TF-VM"), hoja(84, "SUBTD TG-TR2 (TF-AA1)"),
            ]),
            nodo("t37", "TG-TR1 · Iluminación", 75, "Llave general TG-TR1", [
                hoja(71, "Alumbrado 1.er piso (TD-A1)"), hoja(72, "Alumbrado 2.º piso (TD-A2)"),
                hoja(73, "Alumbrado 3.er piso (TD-A3)"), hoja(74, "Alumbrado 4.º piso (TD-A4)"),
            ]),
        ]),
        "fuera": [
            nodo("t34", "TTA-TR1 · Red normal", 67, "No suma al total (criterio del cliente)",
                 [hoja(68, "Red de emergencia")]),
            nodo("t35", "TGE-TR1", 69, "No suma al total (criterio del cliente)",
                 [hoja(70, "Tablero TD-FE")]),
        ],
    },
}


def puntos_de(n: dict) -> list:
    return ([n["punto"]] if n["punto"] else []) + [p for h in n["hijos"] for p in puntos_de(h)]


def p_lima(dt: datetime) -> str:
    return dt.astimezone(LIMA).isoformat(timespec="seconds")


def percentil(vals: list, q: float) -> float:
    if not vals:
        return 0.0
    v = sorted(vals)
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


def bajar_horas(ids: list, ini: datetime, fin: datetime) -> list:
    filas, t, reloj = [], ini, time.monotonic()
    while t < fin:
        t2 = min(t + timedelta(days=TRAMO_DIAS), fin)
        t_tramo = time.monotonic()
        filas += consultar_db("horas", "energia", SQL_HORAS, {"ids": ids, "ini": t, "fin": t2})
        print(f"  {t.astimezone(LIMA):%d-%b} → {t2.astimezone(LIMA):%d-%b %H:%M}: "
              f"{time.monotonic() - t_tramo:.1f} s", flush=True)
        t = t2
    print(f"  {len(filas):,} filas punto-hora en {time.monotonic() - reloj:.0f} s")
    return filas


def series(filas: list, ids: list, t0: datetime, n: int) -> tuple:
    """kWh por hora de cada punto (diferencia del contador entre horas).

    Un hueco reparte su energía en las horas que no tuvieron lecturas; un
    contador que baja (reinicio) o salta (> SALTO_X × su p99) no suma y se
    informa en la calidad de datos.
    """
    por = defaultdict(list)
    for r in filas:
        por[int(r.uid)].append((r.hora, float(r.ep), int(r.n)))
    t0_naive = t0.astimezone(LIMA).replace(tzinfo=None)
    kwh, lect = {}, {}
    calidad = {"saltos": [], "reinicios": [], "horas_repartidas": {}}
    for uid in ids:
        s = sorted(por.get(uid, []))
        k = [0.0] * n
        c = [0] * n
        tramos = []                       # (hora previa, hora, delta)
        previo = None
        for hora, ep, cnt in s:
            i = int((hora - t0_naive).total_seconds() // 3600)
            if 0 <= i < n:
                c[i] = cnt
            if previo is not None:
                tramos.append((previo[0], i, ep - previo[1]))
            previo = (i, ep)
        tasas = [d / (i - j) for j, i, d in tramos if i > j and d > 0]
        p99 = percentil(tasas, 0.99)
        repartidas = 0
        for j, i, d in tramos:
            if i <= j:
                continue
            if d < 0:
                if i >= 0:
                    calidad["reinicios"].append({"punto": uid, "hora": i, "kwh": round(d, 1)})
                continue
            if p99 > 0 and d / (i - j) > SALTO_X * p99 and d > 50:
                if i >= 0:
                    calidad["saltos"].append({"punto": uid, "hora": i, "kwh": round(d, 1)})
                continue
            if i - j > 1:
                repartidas += i - j - 1
            for m in range(j + 1, i + 1):
                if 0 <= m < n:
                    k[m] += d / (i - j)
        kwh[str(uid)] = [round(x, 2) for x in k]
        lect[str(uid)] = c
        if repartidas:
            calidad["horas_repartidas"][str(uid)] = repartidas
    return kwh, lect, calidad


def tarifa(cli: dict) -> dict:
    fila = consultar_db("tarifa", "energia", SQL_TARIFA, {"sede": cli["sede_id"]})
    f = fila[0] if fila else None
    out = {"contratada_kw": float(f.contratada) if f and f.contratada else None}
    if f and f.precio:
        unidad = f.unidad or "kWh"
        por_kwh = float(f.precio) / (1000 if unidad.lower() == "mwh" else 1)
        moneda = {"USD": "US$", "PEN": "S/"}.get((f.moneda or "").upper(), f.moneda or "")
        out.update(moneda=moneda, kwh=por_kwh, precio=float(f.precio), unidad=unidad)
    elif cli["tarifa_ref"]:
        out.update(cli["tarifa_ref"])
    else:
        out.update(moneda="", kwh=None)
    return out


def nota_tarifa(t: dict, ref: dict | None) -> str:
    if ref:
        return ref["nota"]
    if t.get("precio"):
        return f"Con la tarifa registrada en la plataforma: {t['precio']:g} {t['moneda']} por {t['unidad']}."
    return "Sin tarifa registrada: no se puede estimar el costo."


def completar(datos: dict) -> None:
    """Textos fijos de cada cliente: se aplican también con --reusar."""
    for clave, cli in CLIENTES.items():
        c = datos["clientes"].get(clave)
        if not c:
            continue
        c["metodo"] = cli["metodo"]
        if cli["tarifa_ref"]:
            c["tarifa"].update(cli["tarifa_ref"])
        c["tarifa"]["nota"] = nota_tarifa(c["tarifa"], cli["tarifa_ref"])


def bajar() -> dict:
    ahora = datetime.now(timezone.utc)
    hoy = ahora.astimezone(LIMA)
    mes, anio = hoy.month - 2, hoy.year
    if mes < 1:
        mes, anio = mes + 12, anio - 1
    t0 = datetime(anio, mes, 1, tzinfo=LIMA)         # día 1 de hace dos meses
    n = int((ahora - t0).total_seconds() // 3600) + 1
    datos = {"version": 1, "generado": p_lima(ahora), "t0": p_lima(t0), "n_horas": n,
             "clientes": {}}
    ult_global = None
    for clave, cli in CLIENTES.items():
        ids = sorted({p for raiz in [cli["arbol"], *cli["fuera"]] for p in puntos_de(raiz)})
        print(f"\n{cli['empresa']} {cli['sede']}: {len(ids)} puntos")
        filas = bajar_horas(ids, t0 - timedelta(days=1), ahora)   # +1 día de base
        kwh, lect, calidad = series(filas, ids, t0, n)
        ult = consultar_db("ultima", "energia", SQL_ULTIMA,
                           {"ids": ids, "desde": ahora - timedelta(hours=6)})[0].ult
        ult_global = max(filter(None, [ult_global, ult]), default=None)
        datos["clientes"][clave] = {
            "empresa": cli["empresa"], "sede": cli["sede"], "rubro": cli["rubro"],
            "tarifa": tarifa(cli), "arbol": cli["arbol"], "fuera": cli["fuera"],
            "datos_hasta": p_lima(ult) if ult else None,
            "kwh": kwh, "lect": lect, "calidad": calidad}
    datos["datos_hasta"] = p_lima(ult_global) if ult_global else datos["generado"]
    return datos


def total_dia(cli: dict, dia: int) -> float:
    """kWh del total de la sede en el día `dia` (índice desde t0)."""
    raiz = cli["arbol"]
    pts = [raiz["punto"]] if raiz["punto"] else [h["punto"] for h in raiz["hijos"]]
    return sum(sum(cli["kwh"][str(p)][dia * 24:(dia + 1) * 24]) for p in pts)


def validar(datos: dict) -> None:
    """Controles contra cifras conocidas y contra el patrón canónico MAX−MIN."""
    t0 = datetime.fromisoformat(datos["t0"])
    hoy = (datetime.fromisoformat(datos["datos_hasta"]) - t0).days
    print("\n== Controles ==")
    for clave, cli in datos["clientes"].items():
        raiz = cli["arbol"]
        meses = defaultdict(float)
        for d in range(hoy):
            meses[(t0 + timedelta(days=d)).strftime("%Y-%m")] += total_dia(cli, d)
        print(f"{cli['empresa']}: " + " · ".join(f"{m} {v / 1000:,.2f} MWh" for m, v in meses.items())
              + f" (hasta ayer) · tarifa {cli['tarifa'].get('kwh')} {cli['tarifa'].get('moneda')}/kWh")
        # El total debe cubrir a sus tableros (si no, el árbol está mal).
        if raiz["punto"]:
            peor = min((total_dia(cli, d) / max(1e-9, sum(
                sum(cli["kwh"][str(h["punto"])][d * 24:(d + 1) * 24]) for h in raiz["hijos"])), d)
                for d in range(hoy))
            print(f"  llave / Σ tableros: mínimo diario {peor[0]:.2f} "
                  f"({(t0 + timedelta(days=peor[1])):%d-%b}); debe ser ≥ 1")
        for t in raiz["hijos"]:
            if t["punto"] and t["hijos"]:
                tot = sum(cli["kwh"][str(t["punto"])][:hoy * 24])
                hij = sum(sum(cli["kwh"][str(h["punto"])][:hoy * 24]) for h in t["hijos"])
                print(f"  {t['nombre']}: llave / Σ circuitos = {tot / max(hij, 1e-9):.2f}")
        q = cli["calidad"]
        print(f"  saltos {len(q['saltos'])} · reinicios {len(q['reinicios'])} · horas repartidas "
              f"{sum(q['horas_repartidas'].values())}")
        # Control del método: Σ horas de ayer vs MAX−MIN directo del punto principal.
        p = raiz["punto"] or raiz["hijos"][0]["punto"]
        d = hoy - 1
        ini = t0 + timedelta(days=d)
        directo = consultar_db("dia", "energia", SQL_DIA,
                               {"uid": p, "ini": ini, "fin": ini + timedelta(days=1)})[0].kwh
        horas = sum(cli["kwh"][str(p)][d * 24:(d + 1) * 24])
        print(f"  control punto {p} el {ini:%d-%b}: Σ horas {horas:,.1f} kWh vs MAX−MIN "
              f"{float(directo or 0):,.1f} kWh")


def construir(datos: dict) -> str:
    html = PLANTILLA.read_text(encoding="utf-8")
    if any(m not in html for m in (MARCA_ECHARTS, MARCA_H2C, MARCA_DATOS)):
        raise SystemExit("La plantilla no tiene las marcas de ECharts, html2canvas y datos.")
    for marca, ruta in ((MARCA_ECHARTS, ECHARTS), (MARCA_H2C, HTML2CANVAS)):
        lib = ruta.read_text(encoding="utf-8").replace("</script", "<\\/script")
        html = html.replace(marca, f"<script>{lib}</script>", 1)
    js = json.dumps(datos, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return html.replace(MARCA_DATOS, f'<script id="datos" type="application/json">{js}</script>', 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--reusar", action="store_true", help="no consulta: usa el último JSON")
    ap.add_argument("--no-abrir", action="store_true", help="no abre el navegador")
    a = ap.parse_args()
    if a.reusar:
        if not CACHE.exists():
            raise SystemExit(f"No existe {CACHE.relative_to(ROOT)}: corre sin --reusar.")
        datos = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"Datos reusados (generados {datos['generado']}).")
    else:
        datos = bajar()
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        validar(datos)
    completar(datos)
    if not PLANTILLA.exists():
        print(f"\nFalta la plantilla {PLANTILLA.relative_to(ROOT)}: solo se guardaron los datos.")
        return 0
    SALIDA.write_text(construir(datos), encoding="utf-8")
    print(f"\nMaqueta: {SALIDA.relative_to(ROOT)} ({SALIDA.stat().st_size / 1e6:.1f} MB)")
    if not a.no_abrir:
        webbrowser.open(SALIDA.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
