"""Pruebas de la foto de lo peor de hoy y de un día cerrado (sin base ni navegador).

    python -m unittest tests.test_foto_salas -v
"""
from __future__ import annotations

import re
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import foto_salas as fs  # noqa: E402

LIMA = fs.LIMA
DIA = "2026-10-19"
REF = datetime(2026, 10, 19, 15, 0, tzinfo=LIMA)      # "ahora" = 15:00 → n = 900 min
N = 900
FUENTE = {"dia": DIA, "min_actual": N, "congelada": False,
          "max_global": REF.isoformat(), "ref": REF.isoformat()}
GENERADO = datetime(2026, 10, 19, 15, 0, tzinfo=LIMA)
CIERRE = datetime(2026, 10, 20, tzinfo=LIMA)          # 24:00 del lunes 19
FUENTE_CIERRE = {"dia": DIA, "min_actual": 1440, "congelada": False, "cerrado": True,
                 "max_global": (CIERRE + timedelta(minutes=9)).isoformat(),
                 "ref": CIERRE.isoformat()}


def cada(paso: int, desde: int = 0, hasta: int = N, menos=()) -> set:
    """Minutos con lectura cada `paso` min, sin los de `menos`."""
    return {m for m in range(desde, hasta, paso) if m not in set(menos)}


def a_las(minuto: int) -> datetime:
    return datetime(2026, 10, 19, tzinfo=LIMA) + timedelta(minutes=minuto)


def unidad(uid, presentes, cad, tienda="Sanna San Borja", grupo="Zona Roja", nombre="CO2",
           ultima=None):
    if ultima is None and presentes:
        ultima = a_las(max(presentes))
    return fs.Unidad(uid, tienda, grupo, nombre, set(presentes), cad, ultima)


def fila(nombre, estado="ok", falta=None, antig=1.0, ultima=None, tienda="Oechsle Salaverry"):
    return fs.Fila(nombre, f"{tienda} · Tablero", tienda, estado, antig,
                   ultima or (REF - timedelta(minutes=antig or 0)),
                   falta if falta is not None else [0] * N)


def con_corte(ini=459, largo=10):
    return [0] * ini + [1] * largo + [0] * (N - ini - largo)


def fila_cierre(nombre, estado="ok", falta=None, ultima=None):
    """Fila de un día cerrado (1,440 min, medida hasta las 24:00)."""
    ultima = ultima or (CIERRE - timedelta(minutes=1))
    return fs.Fila(nombre, "Oechsle Salaverry · Tablero", "Oechsle Salaverry", estado,
                   (CIERRE - ultima).total_seconds() / 60, ultima,
                   falta if falta is not None else [0] * 1440)


class MinutoAMinuto(unittest.TestCase):
    def test_minimo_para_contar_un_corte(self):
        # energía 1/min, ambiental 5 min, agua 10 y 30 min
        self.assertEqual([fs.minimo_corte(c) for c in (1, 5, 10, 30)], [5, 7, 12, 32])

    def test_cada_lectura_cubre_su_cadencia(self):
        self.assertEqual(sum(fs.minutos_sin_datos(cada(5), N, 5)), 0)

    def test_una_lectura_perdida_suelta_no_cuenta(self):
        self.assertEqual(sum(fs.minutos_sin_datos(cada(5, menos={300}), N, 5)), 0)

    def test_dos_lecturas_seguidas_perdidas_si_cuentan(self):
        falta = fs.minutos_sin_datos(cada(5, menos={300, 305}), N, 5)
        self.assertEqual(fs.cortes(falta), [(300, 10)])

    def test_energia_4_min_no_cuenta_y_5_si(self):
        falta = fs.minutos_sin_datos(cada(1, menos=set(range(100, 104)) | set(range(500, 505))), N, 1)
        self.assertEqual(fs.cortes(falta), [(500, 5)])

    def test_sin_lecturas_desde_hace_rato_es_un_corte_hasta_ahora(self):
        falta = fs.minutos_sin_datos(cada(5, hasta=605), N, 5)
        self.assertEqual(fs.cortes(falta), [(605, 295)])

    def test_tramos_de_10_min(self):
        falta = [0] * 10 + [1] * 3 + [0] * 7 + [1] * 10 + [0] * 5
        self.assertEqual(fs.tramos(falta, total=60),
                         ["ok", "parcial", "falta", "ok", "futuro", "futuro"])

    def test_cadencia_observada_es_la_moda(self):
        self.assertEqual(fs.cadencia_observada(cada(5, menos={100, 200, 205}), 10), 5)
        self.assertEqual(fs.cadencia_observada({0, 30, 60}, 30), 30)    # pocas: la de defecto


class Estado(unittest.TestCase):
    def test_umbrales_por_cadencia(self):
        self.assertEqual(fs.umbrales(1), (5, 15))      # energía, como el monitor
        self.assertEqual(fs.umbrales(5), (12, 30))     # ambiental, como el monitor
        self.assertEqual(fs.umbrales(30), (75, 180))   # agua cada 30 min

    def test_estado_por_ultima_lectura(self):
        self.assertEqual([fs.estado_de(a, 1) for a in (5, 6, 15, 16)],
                         ["ok", "atrasado", "atrasado", "caido"])
        self.assertEqual([fs.estado_de(a, 30) for a in (75, 76, 181)],
                         ["ok", "atrasado", "caido"])
        self.assertEqual(fs.estado_de(None, 5), "caido")


class Filas(unittest.TestCase):
    def test_ambiental_una_fila_por_sala_con_su_peor_sensor(self):
        us = [unidad(1, cada(5, hasta=896), 5),                               # bien
              unidad(2, cada(5, hasta=896, menos={400, 405, 410}), 5),         # corte 15 min
              unidad(3, cada(5, hasta=881), 5),                                # atrasado
              unidad(4, cada(5, hasta=896), 5, grupo="Sala UPS")]
        filas = fs.armar_filas("ambiental", us, N, REF)
        roja, ups = filas
        self.assertEqual((roja.nombre, roja.sub, roja.estado, round(roja.antig)),
                         ("Zona Roja", "Sanna San Borja", "atrasado", 20))
        self.assertEqual(roja.cortes, [(400, 15), (885, 15)])
        self.assertEqual((ups.nombre, ups.gravedad), ("Sala UPS", 3))

    def test_energia_una_fila_por_equipo(self):
        us = [unidad(7, cada(1, hasta=900), 1, tienda="Oechsle Salaverry",
                     grupo="TG-TR2 (TF-AA) - HVAC", nombre="Chiller 1")]
        [f] = fs.armar_filas("energia", us, N, REF)
        self.assertEqual((f.nombre, f.sub, f.gravedad),
                         ("Chiller 1", "Oechsle Salaverry · TG-TR2 (TF-AA) - HVAC", 3))

    def test_agua_medidor_que_dejo_de_enviar(self):
        us = [unidad(9, cada(30, hasta=691), 30, tienda="Scotiabank SITE Barranco",
                     grupo="Principal", nombre="Medidor general")]
        [f] = fs.armar_filas("agua", us, N, REF)
        self.assertEqual((f.nombre, f.sub), ("Principal", "Scotiabank SITE Barranco · Medidor general"))
        self.assertEqual((f.estado, round(f.antig)), ("caido", 210))
        self.assertEqual(fs.detalle(f, DIA), "sin datos desde las 11:30 (hace 3 h 30 min)")

    def test_sin_lecturas_hoy_pero_si_en_dias_anteriores(self):
        u = unidad(5, set(), 1, ultima=datetime(2026, 10, 17, 10, 15, tzinfo=LIMA))
        [f] = fs.armar_filas("energia", [u], N, REF)
        self.assertEqual((f.estado, f.cobertura), ("caido", 0.0))
        self.assertEqual(fs.detalle(f, DIA), "sin datos desde el 17-oct 10:15 (hace 2 días 4 h)")

    def test_orden_peor_primero(self):
        filas = fs.ordenar([
            fila("A bien"),
            fila("B corte chico", falta=con_corte(800, 10)),
            fila("C caida", estado="caido", antig=90.0),
            fila("D corte grande", falta=con_corte(0, 100)),
            fila("E atrasada", estado="atrasado", antig=20.0),
        ])
        self.assertEqual([f.nombre for f in filas],
                         ["C caida", "E atrasada", "D corte grande", "B corte chico", "A bien"])


class Textos(unittest.TestCase):
    def test_detalle_de_cortes_con_horario(self):
        self.assertEqual(fs.detalle(fila("X", falta=con_corte()), DIA), "1 corte: 07:39–07:49 (10 min)")
        dos = [a | b for a, b in zip(con_corte(100, 8), con_corte())]
        self.assertEqual(fs.detalle(fila("X", falta=dos), DIA),
                         "2 cortes; el más largo 07:39–07:49 (10 min)")
        self.assertEqual(fs.detalle(fila("X", estado="atrasado", antig=18.0), DIA),
                         "última lectura a las 14:42 (hace 18 min)")

    def test_cobertura_nunca_dice_100_con_cortes(self):
        self.assertEqual(fs._pct(100.0 * 9997 / 10000), "99.9%")
        self.assertEqual(fs._pct(100.0), "100%")

    def test_todo_bien_solo_texto(self):
        self.assertEqual(fs.caption("ambiental", [fila("A"), fila("B")], FUENTE),
                         "🌡 Ambiental · hoy 15:00\n"
                         "✅ Todo bien: las 2 salas enviaron datos sin cortes hoy.")
        self.assertEqual(fs.caption("energia", [fila("A")], FUENTE).splitlines()[1],
                         "✅ Todo bien: el equipo envió datos sin cortes hoy.")

    def test_resumen_con_problemas(self):
        filas = fs.ordenar([fila("A"), fila("B"), fila("C", falta=con_corte())])
        self.assertEqual(fs.caption("energia", filas, FUENTE),
                         "⚡ Energía · hoy 15:00\n1 de 3 equipos con problemas · ✅ 2 bien")

    def test_lo_que_no_entra_en_la_imagen_va_en_el_texto(self):
        filas = fs.ordenar([fila("Bien"), fila("Caido", estado="caido", antig=90.0),
                            fila("Corte A", falta=con_corte(0, 50)),
                            fila("Corte B", falta=con_corte(0, 20))])
        lineas = fs.caption("energia", filas, FUENTE, max_filas=1).splitlines()
        self.assertEqual(lineas[1:], [
            "3 de 4 equipos con problemas · ✅ 1 bien",
            "⚠️ No entran en la imagen (2):",
            "🟡 Corte A (Oechsle Salaverry): 1 corte: 00:00–00:50 (50 min)",
            "🟡 Corte B (Oechsle Salaverry): 1 corte: 00:00–00:20 (20 min)",
        ])

    def test_texto_nunca_pasa_el_limite_de_telegram(self):
        filas = [fila(f"Equipo con un nombre bastante largo {i:02d}", estado="caido", antig=600.0)
                 for i in range(60)]
        txt = fs.caption("energia", filas, FUENTE)
        self.assertLessEqual(len(txt), 1024)
        self.assertRegex(txt.splitlines()[-1], r"^… y \d+ más$")

    def test_fuente_congelada_lo_advierte(self):
        fuente = dict(FUENTE, congelada=True, max_global="2026-10-12T15:15:00+00:00")
        txt = fs.caption("agua", [fila("A")], fuente)
        self.assertIn("💧 Agua · lunes 19 oct hasta 15:00", txt)
        self.assertIn("no está en vivo: su última lectura es el 12-oct 10:15", txt)

    def test_sin_unidades_activas(self):
        self.assertIn("No hay medidores con datos en los últimos 30 días.",
                      fs.caption("agua", [], FUENTE))


class Imagen(unittest.TestCase):
    def test_solo_aparecen_las_que_tienen_problemas(self):
        filas = fs.ordenar([fila("Sala <UPS> & Co", falta=con_corte(0, 30)),
                            fila("Chiller bien"), fila("Otro bien")])
        h = fs.html_foto("energia", filas, FUENTE, GENERADO)
        self.assertEqual(h.count('class="fila"'), 1)
        self.assertIn("Sala &lt;UPS&gt; &amp; Co", h)
        self.assertNotIn("<UPS>", h)
        self.assertNotIn("Chiller bien", h)
        barras = re.findall(r'<div class="barra">(.*?)</div>', h)
        self.assertEqual([b.count("<i ") for b in barras], [144])
        self.assertEqual(barras[0].count('<i class="falta">'), 3)     # 30 min sin datos
        self.assertIn("3 equipos", h)
        self.assertIn("1 con cortes hoy", h)
        self.assertIn("2 bien", h)
        self.assertNotIn('class="mas"', h)
        self.assertNotIn("no está en vivo", h)

    def test_aviso_al_pie_si_no_entran(self):
        filas = fs.ordenar([fila(f"Equipo {i}", falta=con_corte(0, 10 + i)) for i in range(4)])
        h = fs.html_foto("energia", filas, FUENTE, GENERADO, max_filas=2)
        self.assertEqual(h.count('class="fila"'), 2)
        self.assertIn("Hay 2 equipos más con problemas que no entran en la imagen:", h)
        self.assertIn("Equipo 1 (Oechsle Salaverry), Equipo 0 (Oechsle Salaverry)", h)
        h1 = fs.html_foto("ambiental", filas, FUENTE, GENERADO, max_filas=3)
        self.assertIn("Hay 1 sala más con problemas que no entra en la imagen:", h1)

    def test_aviso_si_la_base_no_esta_en_vivo(self):
        fuente = dict(FUENTE, congelada=True, max_global="2026-10-12T15:15:00+00:00")
        h = fs.html_foto("ambiental", [fila("A", falta=con_corte())], fuente, GENERADO)
        self.assertIn("no está en vivo", h)


class TodoJunto(unittest.TestCase):
    def cargar(self, unidades):
        return mock.patch.dict(fs._CARGAR, {"energia": lambda dia=None: (FUENTE, REF, N, unidades)})

    def test_sin_problemas_no_arma_imagen(self):
        with self.cargar([unidad(1, cada(1, hasta=900), 1)]), \
             mock.patch.object(fs, "png_de_html") as png:
            foto = fs.foto_peores("energia")
        png.assert_not_called()
        self.assertIsNone(foto.png)
        self.assertIn("Todo bien", foto.caption)

    def test_con_problemas_arma_la_imagen(self):
        us = [unidad(1, cada(1, hasta=900), 1),
              unidad(2, cada(1, hasta=900, menos=set(range(300, 320))), 1, nombre="Chiller 1")]
        with self.cargar(us), mock.patch.object(fs, "png_de_html", return_value=b"PNG") as png:
            foto = fs.foto_peores("energia")
        self.assertEqual(foto.png, b"PNG")
        self.assertEqual([f.nombre for f in foto.problemas], ["Chiller 1"])
        self.assertIn("Chiller 1", png.call_args[0][0])
        self.assertIn("1 de 2 equipos con problemas", foto.caption)

    def test_modulo_desconocido(self):
        with self.assertRaises(ValueError):
            fs.foto_peores("gas")

    def test_foto_de_un_dia_cerrado(self):
        us = [unidad(1, cada(1, hasta=1440), 1),
              unidad(2, cada(1, hasta=1080), 1, nombre="Chiller 1")]     # se cortó a las 17:59
        pedidos = []

        def cargar(dia=None):
            pedidos.append(dia)
            return FUENTE_CIERRE, CIERRE, 1440, us

        with mock.patch.dict(fs._CARGAR, {"energia": cargar}), \
             mock.patch.object(fs, "png_de_html", return_value=b"PNG") as png:
            foto = fs.foto_peores("energia", dia=date(2026, 10, 19))
        self.assertEqual(pedidos, [date(2026, 10, 19)])
        self.assertEqual([(f.nombre, f.estado) for f in foto.problemas], [("Chiller 1", "caido")])
        self.assertEqual(foto.caption, "⚡ Energía · cierre del lunes 19 oct\n"
                                       "1 de 2 equipos con problemas · ✅ 1 bien")
        self.assertIn("Cierre del lunes 19 oct", png.call_args[0][0])


class DiaCerrado(unittest.TestCase):
    def test_ventana_del_dia_completo(self):
        maxg = CIERRE + timedelta(minutes=9)
        fuente, ref, n, ini, fin = fs._dia_cerrado(date(2026, 10, 19), maxg,
                                                   ahora=CIERRE + timedelta(minutes=10))
        self.assertEqual((n, ref, ini, fin), (1440, CIERRE, a_las(0), CIERRE))
        self.assertEqual((fuente["dia"], fuente["cerrado"], fuente["congelada"]),
                         (DIA, True, False))

    def test_si_la_base_dejo_de_recibir_datos_se_mide_hasta_ahi(self):
        maxg = a_las(21 * 60 + 30) + timedelta(seconds=40)
        fuente, ref, n, _, _ = fs._dia_cerrado(date(2026, 10, 19), maxg,
                                               ahora=CIERRE + timedelta(hours=8))
        self.assertEqual((fuente["congelada"], ref, n), (True, maxg, 1290))
        txt = fs.caption("energia", [fila_cierre("A", falta=[0] * 1290)], fuente)
        self.assertIn("⚠️ La base no recibe datos desde las 21:30: el día se muestra hasta "
                      "esa hora.", txt)
        h = fs.html_foto("energia", [fila_cierre("A", falta=[0] * 1200 + [1] * 90)], fuente,
                         CIERRE + timedelta(hours=8))
        self.assertIn("de 00:00 a 21:30", h)
        self.assertIn("La base ya no recibía datos", h)

    def test_dia_que_no_termino_o_sin_datos(self):
        with self.assertRaises(ValueError):
            fs._dia_cerrado(date(2026, 10, 19), CIERRE, ahora=CIERRE - timedelta(minutes=1))
        with self.assertRaises(RuntimeError) as ctx:
            fs._dia_cerrado(date(2026, 10, 19), datetime(2026, 10, 12, 10, 15, tzinfo=LIMA),
                            ahora=CIERRE + timedelta(days=1))
        self.assertIn("no tiene datos del lunes 19 oct (su última lectura es el 12-oct 10:15)",
                      str(ctx.exception))

    def test_al_cierre_un_atraso_es_un_corte_mas(self):
        # última lectura 23:50: en vivo sería "con atraso"; al cierre, un corte de 9 min
        [f] = fs.armar_filas("energia", [unidad(1, cada(1, hasta=1431), 1)], 1440, CIERRE,
                             cerrado=True)
        self.assertEqual((f.estado, f.gravedad, f.cortes), ("ok", 2, [(1431, 9)]))
        self.assertEqual(fs.detalle(f, DIA, 1440), "1 corte: 23:51–24:00 (9 min)")
        [g] = fs.armar_filas("energia", [unidad(1, cada(1, hasta=1431), 1)], 1440, CIERRE)
        self.assertEqual(g.estado, "atrasado")

    def test_al_cierre_sin_datos_dice_como_termino_el_dia(self):
        [f] = fs.armar_filas("ambiental", [unidad(1, cada(5, hasta=18 * 60 + 1), 5)], 1440,
                             CIERRE, cerrado=True)
        self.assertEqual((f.nombre, f.estado), ("Zona Roja", "caido"))
        self.assertEqual(fs.detalle(f, DIA, 1440), "sin datos de 18:00 a 24:00 (6 h)")
        u = unidad(2, set(), 1, ultima=datetime(2026, 10, 17, 10, 15, tzinfo=LIMA))
        [g] = fs.armar_filas("energia", [u], 1440, CIERRE, cerrado=True)
        self.assertEqual(fs.detalle(g, DIA, 1440),
                         "sin datos en todo el día (desde el 17-oct 10:15)")

    def test_texto_del_cierre(self):
        self.assertEqual(fs.caption("energia", [fila_cierre("A"), fila_cierre("B")], FUENTE_CIERRE),
                         "⚡ Energía · cierre del lunes 19 oct\n"
                         "✅ Todo bien: los 2 equipos enviaron datos sin cortes en todo el día.")
        caido = fila_cierre("Caido", estado="caido", ultima=a_las(18 * 60),
                            falta=[0] * 1081 + [1] * 359)
        filas = fs.ordenar([fila_cierre("A"), caido])
        self.assertEqual(fs.caption("energia", filas, FUENTE_CIERRE, max_filas=0).splitlines()[1:], [
            "1 de 2 equipos con problemas · ✅ 1 bien",
            "⚠️ No entran en la imagen (1):",
            "🔴 Caido (Oechsle Salaverry): sin datos de 18:00 a 24:00 (6 h)",
        ])

    def test_imagen_del_cierre(self):
        caido = fila_cierre("Caido", estado="caido", ultima=a_las(18 * 60),
                            falta=[0] * 1081 + [1] * 359)
        cortes = fila_cierre("Con corte", falta=[0] * 600 + [1] * 30 + [0] * 810)
        filas = fs.ordenar([caido, cortes, fila_cierre("Bien")])
        h = fs.html_foto("energia", filas, FUENTE_CIERRE, CIERRE + timedelta(minutes=10))
        self.assertIn("Cierre del lunes 19 oct · de 00:00 a 24:00 (hora Lima)", h)
        self.assertIn("1 sin datos al cierre", h)
        self.assertIn("1 con cortes<", h)
        self.assertIn("Sin datos de 18:00 a 24:00 (6 h)", h)
        self.assertNotIn('class="futuro"', h)          # día completo: nada en gris
        self.assertNotIn("Todavía no pasa", h)
        self.assertIn("generado el 20-oct 00:10", h)

    def test_cargar_un_dia_cerrado_con_el_inventario_de_hoy(self):
        def u(uid, nombre, activo=True):
            return {"uid": uid, "tienda": "Oechsle Salaverry", "grupo": "TG", "nombre": nombre,
                    "activo": activo, "cadencia_min": 1}

        est = {"fuente": {"max_global": (CIERRE + timedelta(hours=9)).isoformat()},
               "unidades": [u(1, "Reportó ese día"), u(2, "Mudo ese día, no antes"),
                            u(3, "Nada en 30 días"), u(4, "Inactivo", activo=False)]}
        lecturas = {"rows": [{"uid": 1, "dia": DIA, "hora": m // 60, "minuto": m % 60, "n": 1}
                             for m in range(1440)]}
        previa = datetime(2026, 10, 18, 22, 0, tzinfo=LIMA)
        with mock.patch.object(fs.monitor, "lecturas", return_value=lecturas) as lect, \
             mock.patch.object(fs, "ultimas_lecturas", return_value={2: previa}) as ult:
            fuente, ref, n, us = fs._cargar_monitor_cerrado("energia", est, date(2026, 10, 19))
        self.assertEqual(lect.call_args[0], ("energia", [1, 2, 3, 4], DIA, DIA))
        self.assertEqual(ult.call_args[0][:3], ("energia", [2, 3], CIERRE))   # solo lo activo
        self.assertEqual(ult.call_args[1], {"fin": CIERRE})
        self.assertEqual([(x.uid, len(x.presentes), x.ultima) for x in us],
                         [(1, 1440, a_las(1439)), (2, 0, previa)])
        self.assertEqual((fuente["cerrado"], ref, n), (True, CIERRE, 1440))


if __name__ == "__main__":
    unittest.main()
