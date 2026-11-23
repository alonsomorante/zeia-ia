"""Pruebas del reporte manual de alertas (sin base de datos).

    python -m unittest tests.test_reporte_alertas -v
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

RUTA = Path(__file__).resolve().parent.parent / "scripts" / "reporte_alertas.py"
_spec = importlib.util.spec_from_file_location("reporte_alertas", RUTA)
ra = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ra          # dataclass necesita el módulo registrado
_spec.loader.exec_module(ra)

LIMA = ra.LIMA


def _dt(dia: int, hora: int, minuto: int = 0) -> datetime:
    return datetime(2026, 10, dia, hora, minuto, tzinfo=LIMA)


def _evento(cuando: datetime, punto: str = "Llave general", tipo: str = "Fluctuación de voltaje",
            severidad: str = "Crítica", modulo: str = "energia", empresa: str = "Sanna",
            valor: float = 210.0, reportada: bool = True, correo: bool = True,
            dest=("jefe@zeia.com.pe",), resumen: bool = False) -> object:
    return ra.Evento(
        modulo=modulo, empresa=empresa, sede="San Borja", grupo="Tablero TG",
        punto=punto, tipo=tipo, subtipo="Subtensión", severidad=severidad,
        cuando=cuando, valor=valor, unidad="V", nota="Voltaje por debajo del umbral",
        destinatarios=dest, correo_activo=correo, reportada=reportada,
        resumen=resumen, resumen_cuando=None,
    )


def _limite(cuando: datetime, sala: str = "Sala técnica", nivel: str = "UNHEALTHY",
            valor: float = 1200.0, resuelto: bool = False):
    return ra.Limite(
        empresa="Sanna", sede="San Borja", sala=sala, equipo="CO2-sensor #1",
        indicador="CO₂", nivel=nivel, cuando=cuando, valor=valor,
        unidad="ppm", medicion="AVERAGE", resuelto=resuelto,
    )


def _hueco(inicio: datetime, fin: datetime, minutos: float | None = None):
    return ra.Hueco(
        empresa="Sanna", sede="San Borja", sala="Sala técnica", equipo="Multisensor #7",
        inicio=inicio, fin=fin,
        minutos=minutos if minutos is not None else (fin - inicio).total_seconds() / 60,
        abierto=False, venia_de_antes=False,
        destinatarios=("jesus@zeia.com.pe",), correo_activo=True,
    )


def _config() -> dict:
    return {
        "umbrales": [ra.ConfigUmbral("Sanna", ("jefe@zeia.com.pe",), True,
                                     "Fluctuación de voltaje (3)", 3)],
        "programados": [ra.ConfigProgramado("Oechsle", "Resumen apertura/cierre",
                                            "12:05 · Apertura", ("jefe@zeia.com.pe",),
                                            True, 2)],
        "nodata": [{"punto": "Llave general", "empresa": "Sanna", "sede": "San Borja",
                    "timeout": 60, "recordatorio": 120, "correo": ("jefe@zeia.com.pe",),
                    "activo": True, "estado": "down", "caido_desde": _dt(10, 8),
                    "ultimo_aviso": _dt(10, 9), "abierto": True}],
        "ambiental": [{"equipo": "CO2-sensor #1", "empresa": "Sanna", "sede": "San Borja",
                       "sala": "Sala técnica", "timeout": 60, "recordatorio": 0,
                       "correo": ("jesus@zeia.com.pe",), "estado": "", "caido_desde": None,
                       "ultimo_aviso": None, "activo": True, "abierto": False}],
    }


def _datos() -> tuple:
    """Dos semanas: energía repetida, un episodio de agua y Limit Alerts."""
    eventos = [
        # 3 alertas seguidas (≤ 15 min) -> 1 episodio de 3
        _evento(_dt(5, 10, 0)), _evento(_dt(5, 10, 5)), _evento(_dt(5, 10, 10)),
        # 40 min después -> otro episodio
        _evento(_dt(5, 10, 50)),
        # segunda semana
        _evento(_dt(12, 15, 0), punto="Tomógrafo", severidad="Moderada",
                reportada=False, correo=False),
        _evento(_dt(12, 15, 5), punto="Tomógrafo", severidad="Moderada",
                reportada=False, correo=False),
        _evento(_dt(13, 9, 0), modulo="agua", empresa="Scotiabank", punto="Medidor general",
                tipo="Consumo de agua promedio", valor=1.2, severidad="Crítica"),
    ]
    limites = [
        _limite(_dt(5, 11, 0)), _limite(_dt(5, 11, 30)), _limite(_dt(6, 11, 0), nivel="DANGEROUS"),
        _limite(_dt(12, 12, 0), sala="Oficina"),
    ]
    huecos = [_hueco(_dt(6, 8), _dt(6, 10)),                 # 120 min
              _hueco(_dt(12, 18), _dt(12, 21), minutos=180)]
    return eventos, limites, huecos


def _comparativas(d1: date, d2: date) -> list:
    return [
        {"etiqueta": f"Periodo del reporte: {ra.fmt_fecha_corta(d1)}–{ra.fmt_fecha_corta(d2)}",
         "energia": 5, "agua": 1, "ambiental": 4, "total": 10},
        {"etiqueta": "Periodo anterior: 28/09–04/10", "energia": 2, "agua": 0,
         "ambiental": 1, "total": 3},
        {"etiqueta": "4 semanas antes: 07/09–13/09", "energia": 0, "agua": 0,
         "ambiental": 0, "total": 0},
    ]


def _html(detalle: bool = False, d1: date = date(2026, 10, 5),
          d2: date = date(2026, 10, 18), empresas=None) -> str:
    eventos, limites, huecos = _datos()
    return ra.armar_html(d1, d2, eventos, limites, huecos, _config(),
                         _comparativas(d1, d2), _dt(19, 7), ra.CORTE_MIN,
                         empresas=empresas, detalle=detalle)


class TestFormato(unittest.TestCase):
    def test_miles_y_decimales_al_estilo_peruano(self):
        self.assertEqual(ra.fmt_n(2108), "2.108")
        self.assertEqual(ra.fmt_dec(1234.567, 1), "1.234,6")
        self.assertEqual(ra.fmt_dec(None), "—")

    def test_duracion(self):
        self.assertEqual(ra.fmt_duracion(45), "45 min")
        self.assertEqual(ra.fmt_duracion(90), "1 h 30 min")
        self.assertEqual(ra.fmt_duracion(120), "2 h")
        self.assertEqual(ra.fmt_duracion(60 * 26), "1 d 2 h")

    def test_destinatarios_sin_repetidos_ni_basura(self):
        self.assertEqual(ra.parse_destinatarios("A@X.com, b@y.com; A@x.com  "),
                         ("a@x.com", "b@y.com"))
        self.assertEqual(ra.parse_destinatarios(""), ())
        self.assertEqual(ra.parse_destinatarios("no-es-correo"), ())

    def test_rango_largo(self):
        self.assertEqual(ra.rango_largo(date(2026, 11, 1), date(2026, 11, 1)),
                         "1 de noviembre de 2026")
        self.assertEqual(ra.rango_largo(date(2026, 10, 26), date(2026, 11, 1)),
                         "26 de octubre al 1 de noviembre de 2026")

    def test_semana_pasada_y_corte_por_semanas_iso(self):
        self.assertEqual(ra.semana_pasada(date(2026, 11, 13)),
                         (date(2026, 11, 2), date(2026, 11, 8)))
        self.assertEqual(ra.cortar_semanas(date(2026, 11, 2), date(2026, 11, 8)),
                         [(date(2026, 11, 2), date(2026, 11, 8))])
        self.assertEqual(ra.cortar_semanas(date(2026, 11, 5), date(2026, 11, 18)),
                         [(date(2026, 11, 5), date(2026, 11, 8)),
                          (date(2026, 11, 9), date(2026, 11, 15)),
                          (date(2026, 11, 16), date(2026, 11, 18))])


class TestEpisodios(unittest.TestCase):
    def test_agrupa_repetidas_dentro_del_corte(self):
        eventos, _, _ = _datos()
        eps = ra.agrupar_episodios(eventos, 15)
        energia = [e for e in eps if e.modulo == "energia" and e.punto == "Llave general"]
        self.assertEqual(len(energia), 2)                 # 10:00–10:10 y 10:50
        self.assertEqual(energia[0].n, 3)
        self.assertEqual(energia[0].minutos, 10)

    def test_corte_cero_deja_cada_alerta_suelta(self):
        eventos, _, _ = _datos()
        eps = ra.agrupar_episodios(eventos, 0)
        self.assertEqual(len([e for e in eps if e.modulo == "energia"]), 6)

    def test_severidad_y_banderas_se_unen(self):
        eventos = [_evento(_dt(5, 10, 0), severidad="Moderada", reportada=False,
                           correo=False, dest=("a@x.com",)),
                   _evento(_dt(5, 10, 5), severidad="Crítica", reportada=True,
                           correo=True, dest=("b@x.com",), resumen=True)]
        ep, = ra.agrupar_episodios(eventos, 15)
        self.assertEqual(ep.severidad, "Crítica")
        self.assertEqual(ep.reportadas, 1)
        self.assertEqual(ep.notificacion, "Parcial")
        self.assertTrue(ep.correo_activo)
        self.assertEqual(ep.destinatarios, ("a@x.com", "b@x.com"))
        self.assertEqual(ep.resumen, 1)

    def test_puntos_distintos_no_se_mezclan(self):
        eventos = [_evento(_dt(5, 10, 0)), _evento(_dt(5, 10, 1), punto="Tomógrafo")]
        self.assertEqual(len(ra.agrupar_episodios(eventos, 15)), 2)


class TestLimites(unittest.TestCase):
    def test_agrupa_por_sala_equipo_indicador_y_nivel(self):
        _, limites, _ = _datos()
        dias = [date(2026, 10, 5) + timedelta(days=i) for i in range(14)]
        grupos = ra.agrupar_limites(limites, dias)
        self.assertEqual(len(grupos), 3)                 # UNHEALTHY, DANGEROUS y Oficina
        unhealty = [g for g in grupos if g.sala == "Sala técnica" and g.nivel == "UNHEALTHY"]
        self.assertEqual(len(unhealty), 1)
        self.assertEqual(unhealty[0].n, 2)
        self.assertEqual(unhealty[0].dias[0], 2)         # lunes 5
        self.assertEqual((unhealty[0].valor_min, unhealty[0].valor_max), (1200.0, 1200.0))

    def test_severidad_ambiental(self):
        self.assertEqual(ra.severidad_ambiental("CRITICAL"), "Crítica")
        self.assertEqual(ra.severidad_ambiental("UNHEALTHY"), "Alta")
        self.assertEqual(ra.severidad_ambiental("MODERATE"), "Media")
        self.assertEqual(ra.severidad_ambiental("GOOD"), "Baja")


class TestHTML(unittest.TestCase):
    def test_portada_con_kpis_y_totales(self):
        doc = _html()
        self.assertIn("Alertas y correos del periodo", doc)
        self.assertIn("5 al 18 de octubre de 2026", doc)
        self.assertIn("Correos del periodo", doc)
        self.assertIn("Alertas registradas", doc)
        self.assertIn("Cómo se lee", doc)
        self.assertIn("Alertas vs correos por módulo", doc)
        self.assertIn("2. Energía", doc)
        self.assertIn("3. Agua", doc)
        self.assertIn("4. Ambiental", doc)
        self.assertIn("Sanna", doc)
        self.assertIn("Scotiabank", doc)

    def test_resumen_y_tres_modulos_sin_anexos(self):
        doc = _html()
        self.assertEqual(doc.count('<section class="modulo">'), 3)
        self.assertIn("2. Energía", doc)
        self.assertIn("3. Agua", doc)
        self.assertIn("4. Ambiental", doc)
        self.assertNotIn("Anexo A", doc)
        self.assertNotIn("Configuración de correos", doc)
        self.assertNotIn("Metodología y limitaciones", doc)

    def test_ambiental_por_dia_y_huecos_inferidos(self):
        eventos, limites, huecos = _datos()
        correos = [
            ra.Correo(
                sistema="Ambiental", tipo="Sin datos", evidencia="Inferido",
                dia=h.inicio.date(), hora=h.inicio.time(), empresa=h.empresa,
                sede=h.sede, detalle=f"{h.sala} · {h.equipo}", n=1,
                destinatarios=h.destinatarios,
                nota=f"{ra.fmt_duracion(h.minutos)} sin lecturas · cerrado",
            )
            for h in huecos
        ]
        doc = ra.armar_html(date(2026, 10, 5), date(2026, 10, 18), eventos,
                            limites, huecos, _config(),
                            _comparativas(date(2026, 10, 5), date(2026, 10, 18)),
                            _dt(19, 7), ra.CORTE_MIN, correos=correos)
        self.assertIn("4. Ambiental", doc)
        self.assertIn("Inferidos", doc)
        self.assertIn("Sanna", doc)
        # El hueco de 3 h aparece en el detalle de correos de la empresa.
        self.assertIn("3 h", doc)
        self.assertIn("Correos, día por día", doc)

    def test_filtro_por_empresa(self):
        doc = _html(empresas=["Scotiabank"])
        self.assertIn("Scotiabank", doc)
        self.assertNotIn("Sanna ·", doc)
        self.assertNotIn("Oechsle", doc)

    def test_empresa_de_prueba_fuera_del_filtro(self):
        eventos, _, _ = _datos()
        self.assertEqual(ra.filtrar_empresas(eventos, ["sanna"]),
                         [e for e in eventos if e.empresa == "Sanna"])
        self.assertEqual(len(ra.filtrar_empresas(eventos, None)), len(eventos))

    def test_sin_datos_no_rompe(self):
        doc = ra.armar_html(date(2026, 11, 2), date(2026, 11, 8), [], [], [], _config(),
                            _comparativas(date(2026, 11, 2), date(2026, 11, 8)),
                            _dt(9, 7), 15)
        self.assertIn("Sin alertas ni correos en el periodo", doc)
        self.assertIn("2. Energía", doc)
        self.assertIn("4. Ambiental", doc)


class TestSQL(unittest.TestCase):
    def test_todas_las_consultas_pasan_la_validacion_de_solo_lectura(self):
        for sql in (ra.SQL_ENERGIA, ra.SQL_AGUA, ra.SQL_AMBIENTAL,
                    ra.SQL_AMBIENTAL_DEVICES, ra.SQL_UMBRALES,
                    ra.SQL_PROGRAMADOS_STATUS, ra.SQL_PROGRAMADOS_DIARIO,
                    ra.SQL_PROGRAMADOS_MENSUAL, ra.SQL_NODATA_ENERGIA):
            ra.validate_sql(sql)

    def test_bounds_en_hora_de_lima(self):
        # La trampa conocida: comparar created_at contra un ::date pelado corta
        # el día a las 19:00 de Lima. Los bounds deben venir ya en hora local.
        ini = ra.inicio_dia(date(2026, 11, 2))
        fin = ra.fin_dia(date(2026, 11, 2))
        self.assertEqual(ini.utcoffset(), timedelta(hours=-5))
        self.assertEqual(ini.hour, 0)
        self.assertEqual(fin - ini, timedelta(days=1))


class TestPDF(unittest.TestCase):
    def test_pdf_listo_exige_eof_y_tamano_estable(self):
        import tempfile
        from src.reporte_pdf import _pdf_listo

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "x.pdf"
            pdf.write_bytes(b"%PDF-1.4\ncontenido\n%%EOF\n")
            listo, tam = _pdf_listo(pdf, -1)
            self.assertTrue(tam > 0)
            self.assertFalse(listo)                  # primera pasada: aún no
            listo, tam = _pdf_listo(pdf, tam)
            self.assertTrue(listo)                   # mismo tamaño + %%EOF
            pdf.write_bytes(b"%PDF-1.4\ncontenido\n")   # sin cierre
            listo, _ = _pdf_listo(pdf, -1)
            self.assertFalse(listo)


class _Fila:
    def __init__(self, **kw):
        self._mapping = kw


class TestCarga(unittest.TestCase):
    def test_energia_normaliza_hora_lima_destinatarios_y_subtipo(self):
        utc = ra.timezone.utc
        fila = _Fila(
            id=1, cuando=datetime(2026, 11, 5, 15, 5, tzinfo=utc),  # 10:05 Lima
            reported=True, reported_at=None, email_summary_reported=True,
            resumen_cuando=datetime(2026, 11, 5, 17, 0, tzinfo=utc),
            alert_status="critical", value=210.5, notes="Voltaje bajo",
            fluctuation_subtype="undervoltage", current_subtype=None,
            power_subtype=None, energy_subtype=None, unbalanced_subtype=None,
            alert_type="voltage_fluctuation", email_sending=True,
            recips="A@X.com, b@y.com; A@x.com", empresa="Sanna", sede="San Borja",
            tablero="TG", punto="Llave general",
        )
        with mock.patch.object(ra, "consultar", lambda *a, **k: [fila]):
            eventos = ra.cargar_energia(ra.inicio_dia(date(2026, 11, 5)),
                                        ra.fin_dia(date(2026, 11, 5)))
        ev, = eventos
        self.assertEqual(ev.cuando.hour, 10)                 # UTC-5
        self.assertEqual(ev.cuando.tzinfo, ra.LIMA)
        self.assertEqual(ev.subtipo, "Subtensión")
        self.assertEqual(ev.severidad, "Crítica")
        self.assertEqual(ev.destinatarios, ("a@x.com", "b@y.com"))
        self.assertTrue(ev.correo_activo)
        self.assertTrue(ev.resumen)
        self.assertEqual(ev.resumen_cuando.hour, 12)

    def test_energia_ignora_empresas_de_prueba(self):
        fila = _Fila(
            id=1, cuando=datetime(2026, 11, 5, 15, 0, tzinfo=ra.timezone.utc),
            reported=False, reported_at=None, email_summary_reported=False,
            resumen_cuando=None, alert_status="moderate", value=1.0, notes="",
            fluctuation_subtype=None, current_subtype=None, power_subtype=None,
            energy_subtype=None, unbalanced_subtype=None,
            alert_type="voltage_fluctuation", email_sending=False, recips="",
            empresa="TestCorp_Email", sede="", tablero="", punto="PM",
        )
        with mock.patch.object(ra, "consultar", lambda *a, **k: [fila]):
            self.assertEqual(ra.cargar_energia(ra.inicio_dia(date(2026, 11, 5)),
                                               ra.fin_dia(date(2026, 11, 5))), [])

    def test_agua_arma_la_nota_si_la_plataforma_no_la_trae(self):
        fila = _Fila(
            id=1, cuando=datetime(2026, 10, 23, 12, 0, tzinfo=ra.timezone.utc),
            value_m3=1.5, average_m3=1.0, alert_status="critical", notes="",
            reported=True, reported_at=None, email_sending=True,
            recips="jefe@zeia.com.pe", empresa="Scotiabank", sede="Jockey Plaza",
            tuberia="Kitchenette", medidor="Medidor general",
        )
        with mock.patch.object(ra, "consultar", lambda *a, **k: [fila]):
            ev, = ra.cargar_agua(ra.inicio_dia(date(2026, 10, 23)),
                                 ra.fin_dia(date(2026, 10, 23)))
        self.assertIn("1,500 m³", ev.nota)
        self.assertIn("promedio 1,000 m³", ev.nota)
        self.assertEqual(ev.unidad, "m³")

    def test_ambiental_conserva_el_nivel_crudo(self):
        fila = _Fila(
            id=9, cuando=datetime(2026, 11, 5, 15, 0, tzinfo=ra.timezone.utc),
            device_id=181, indicator="CO2", level="UNHEALTHY", type_value="AVERAGE",
            value=1200, unit="PPM", resolved=False, sala="Sala técnica",
            sede="San Borja", empresa="Sanna", tipo_sensor="TS-201-TH",
        )
        with mock.patch.object(ra, "consultar", lambda *a, **k: [fila]):
            lim, = ra.cargar_ambiental(ra.inicio_dia(date(2026, 11, 5)),
                                       ra.fin_dia(date(2026, 11, 5)))
        self.assertEqual(lim.nivel, "UNHEALTHY")
        self.assertEqual(ra.severidad_ambiental(lim.nivel), "Alta")
        self.assertEqual(lim.indicador, "CO₂")
        self.assertEqual(lim.unidad, "ppm")


class TestHuecos(unittest.TestCase):
    """Los correos de sin datos ambiental se infieren de los huecos."""

    DEV = dict(id=1, tipo_sensor="Multisensor", no_data_alert=True,
               timeout_minutes=60, reminder_minutes=0, recips="jesus@zeia.com.pe",
               no_data_status="", down_started_at=None, last_missing_alert_sent=None,
               is_activated=True, sala="Sala A", sede="San Borja", empresa="Sanna",
               sala_activa=True)

    def _cargar(self, lecturas, previo):
        def consultar(base, sql, **params):
            if sql is ra.SQL_AMBIENTAL_DEVICES:
                return [_Fila(**self.DEV)]
            if "equipments_indicatordevice" in sql:
                return [(10,)]
            if "date_trunc" in sql:
                return [(t,) for t in lecturas]
            if "max(created_at)" in sql:
                return [(previo,)]
            raise AssertionError(sql[:60])
        with mock.patch.object(ra, "consultar", consultar):
            return ra.cargar_huecos(date(2026, 11, 2), date(2026, 11, 2))

    def test_hueco_mayor_al_timeout(self):
        horas = [ra.datetime(2026, 11, 2, h, 0, tzinfo=ra.LIMA) for h in range(24)]
        horas.remove(ra.datetime(2026, 11, 2, 6, 0, tzinfo=ra.LIMA))
        horas.remove(ra.datetime(2026, 11, 2, 7, 0, tzinfo=ra.LIMA))
        huecos = self._cargar(horas, ra.datetime(2026, 11, 1, 23, 50, tzinfo=ra.LIMA))
        self.assertEqual(len(huecos), 1)
        h, = huecos
        self.assertEqual((h.inicio.hour, h.fin.hour), (5, 8))
        self.assertEqual(h.minutos, 180)
        self.assertFalse(h.abierto)

    def test_hueco_igual_al_timeout_no_aparece(self):
        # Lecturas cada 15 min; faltan 06:00, 06:15 y 06:30 -> hueco de 60 min
        # exactos, que es el timeout: el aviso solo salta si lo supera.
        base = ra.datetime(2026, 11, 2, tzinfo=ra.LIMA)
        lecturas = [base + timedelta(minutes=15 * i) for i in range(96)]
        lecturas = [t for t in lecturas if not (t.hour == 6 and t.minute in (0, 15, 30))]
        self.assertEqual(self._cargar(lecturas, ra.datetime(2026, 11, 1, 23, 55,
                                                            tzinfo=ra.LIMA)), [])

    def test_equipo_que_nunca_reporto_se_marca(self):
        huecos = self._cargar([], None)
        self.assertEqual(len(huecos), 1)
        self.assertTrue(huecos[0].sin_lecturas)
        self.assertTrue(huecos[0].abierto)
        self.assertEqual(huecos[0].minutos, 1440)


class TestCorreos(unittest.TestCase):
    """Correos del periodo: registrados, programados e inferidos."""

    ESTADO = [
        {"id": 1, "report_time": time(12, 5), "is_active": True, "should_be_on": True,
         "descripcion": "Apertura de tienda", "recips": "a@x.com, b@y.com",
         "empresa": "Oechsle", "sede": "Salaverry", "punto": "Llave general TG-TR1"},
        {"id": 2, "report_time": time(12, 5), "is_active": True, "should_be_on": True,
         "descripcion": "Apertura de tienda", "recips": "a@x.com",
         "empresa": "Oechsle", "sede": "Salaverry", "punto": "TG-TR2 (TF-AA)"},
        {"id": 3, "report_time": time(21, 5), "is_active": True, "should_be_on": False,
         "descripcion": "Cierre de tienda", "recips": "a@x.com",
         "empresa": "Oechsle", "sede": "Salaverry", "punto": "Llave general TG-TR1"},
        {"id": 4, "report_time": time(12, 5), "is_active": False, "should_be_on": True,
         "descripcion": "Apertura de tienda", "recips": "a@x.com",
         "empresa": "Oechsle", "sede": "Salaverry", "punto": "Red normal"},
    ]
    DIARIO = [
        {"id": 1, "is_active": True, "description": "", "recips": "s@x.com",
         "empresa": "Sanna"},
        {"id": 2, "is_active": True, "description": "", "recips": "s@x.com",
         "empresa": "Scotiabank"},
        {"id": 3, "is_active": False, "description": "", "recips": "s@x.com",
         "empresa": "KFC"},
    ]
    MENSUAL = [
        {"id": 1, "is_active": True, "description": "", "recips": "m@x.com",
         "empresa": "Scotiabank"},
    ]

    def _config(self):
        return {"umbrales": [], "programados": [], "nodata": [], "ambiental": [],
                "ultimo_resumen": _dt(26, 17)}

    def _cargar(self, d1, d2, eventos=(), huecos=(), resumenes=(), limites=()):
        def fake(base, sql, **params):
            if sql is ra.SQL_RESUMENES:
                return [_Fila(momento=m, n=n) for m, n in resumenes]
            if sql is ra.SQL_ESTADO_ACTUAL:
                return [_Fila(**f) for f in self.ESTADO]
            if sql is ra.SQL_PROGRAMADOS_DIARIO:
                return [_Fila(**f) for f in self.DIARIO]
            if sql is ra.SQL_PROGRAMADOS_MENSUAL:
                return [_Fila(**f) for f in self.MENSUAL]
            raise AssertionError(sql[:60])
        with unittest.mock.patch.object(ra, "consultar", fake):
            return ra.cargar_correos(d1, d2, list(eventos), list(limites),
                                     list(huecos), self._config())

    def test_alerta_registrada_usa_la_hora_del_envio(self):
        ev = _evento(_dt(5, 10, 0))
        ev.reportada_cuando = _dt(5, 10, 2)
        correos = self._cargar(date(2026, 10, 5), date(2026, 10, 5), [ev])
        c = [x for x in correos if x.tipo == "Alerta"][0]
        self.assertEqual(c.evidencia, "Registrado")
        self.assertEqual(c.hora, time(10, 2))
        self.assertEqual(c.detalle, "Llave general · Fluctuación de voltaje")
        self.assertEqual(c.n, 1)

    def test_alerta_no_reportada_no_es_correo(self):
        ev = _evento(_dt(5, 10, 0), reportada=False)
        correos = self._cargar(date(2026, 10, 5), date(2026, 10, 5), [ev])
        self.assertEqual([c for c in correos if c.tipo == "Alerta"], [])

    def test_alerta_reportada_al_dia_siguiente_queda_en_el_dia_del_envio(self):
        ev = _evento(_dt(8, 23, 30))
        ev.reportada_cuando = _dt(9, 0, 5)              # día siguiente, fuera del rango
        correos = self._cargar(date(2026, 10, 5), date(2026, 10, 8), [ev])
        c = [x for x in correos if x.tipo == "Alerta"][0]
        self.assertEqual(c.dia, date(2026, 10, 9))
        self.assertEqual(c.hora, time(0, 5))
        self.assertIn("alerta de", c.nota)
        doc = ra.armar_html(date(2026, 10, 5), date(2026, 10, 8), [], [], [], _config(),
                            _comparativas(date(2026, 10, 5), date(2026, 10, 8)),
                            _dt(10, 7), ra.CORTE_MIN, correos=correos)
        self.assertIn("09 oct", doc)                    # no se pierde el correo

    def test_resumen_diario_registrado(self):
        correos = self._cargar(
            date(2026, 10, 5), date(2026, 10, 5), resumenes=[(_dt(5, 17), 42)])
        c, = [x for x in correos if x.tipo == "Resumen diario de alertas"]
        self.assertEqual(c.evidencia, "Registrado")
        self.assertEqual(c.hora, time(17, 0))
        self.assertIn("42", c.detalle)

    def test_estado_actual_agrupa_puntos_por_hora_y_omite_inactivos(self):
        # 2 puntos a las 12:05 + 1 a las 21:05; el inactivo no entra.
        correos = self._cargar(date(2026, 10, 5), date(2026, 10, 5))
        estado = [c for c in correos if c.tipo.startswith("Estado actual")]
        self.assertEqual(len(estado), 2)
        apertura = [c for c in estado if c.hora == time(12, 5)][0]
        self.assertEqual(apertura.n, 1)                  # UN correo con 2 puntos
        self.assertIn("2 puntos", apertura.detalle)
        self.assertIn("Llave general TG-TR1", apertura.detalle)
        self.assertIn("TG-TR2", apertura.detalle)
        self.assertNotIn("Red normal", apertura.detalle)
        self.assertEqual(apertura.evidencia, "Programado")
        self.assertEqual(apertura.destinatarios, ("a@x.com", "b@y.com"))

    def test_estado_actual_se_repite_cada_dia(self):
        correos = self._cargar(date(2026, 10, 5), date(2026, 10, 7))
        self.assertEqual(len([c for c in correos if c.tipo == "Estado actual (apertura)"]), 3)

    def test_cobertura_diaria_y_consumo_mensual(self):
        correos = self._cargar(date(2026, 11, 1), date(2026, 11, 2))
        cobertura = [c for c in correos if c.tipo == "Cobertura diaria"]
        self.assertEqual(len(cobertura), 4)              # 2 empresas x 2 días
        mensual = [c for c in correos if c.tipo == "Consumo mensual"]
        self.assertEqual(len(mensual), 1)                # solo el día 1
        self.assertEqual(mensual[0].empresa, "Scotiabank")
        self.assertIsNone(mensual[0].hora)

    def test_hueco_ambiental_es_correo_inferido(self):
        correos = self._cargar(date(2026, 10, 6), date(2026, 10, 6),
                               huecos=[_hueco(_dt(6, 8), _dt(6, 10))])
        c = [x for x in correos if x.evidencia == "Inferido"][0]
        self.assertEqual(c.tipo, "Sin datos")
        self.assertEqual(c.hora, time(8, 0))
        self.assertIn("2 h", c.nota)
        self.assertEqual(c.destinatarios, ("jesus@zeia.com.pe",))

    def _limite_amb(self, indicador: str, nivel: str = "HUMIDITY_MAX",
                    empresa: str = "Sanna") -> object:
        return ra.Limite(
            empresa=empresa, sede="San Borja", sala="Sala A", equipo="AM 103 #1",
            indicador=indicador, nivel=nivel, cuando=_dt(5, 10, 0), valor=70.0,
            unidad="%", medicion="Instantáneo", resuelto=False,
        )

    def test_ambiental_humedad_y_temperatura_generan_correo(self):
        # Regla de Sanna: humedad y temperatura sí; CO₂ no.
        correos = self._cargar(
            date(2026, 10, 5), date(2026, 10, 5),
            limites=[self._limite_amb("Humedad"),
                     self._limite_amb("Temperatura", "TEMP_MAX"),
                     self._limite_amb("CO₂", "UNHEALTHY")])
        amb = [c for c in correos if c.sistema == "Ambiental"]
        self.assertEqual(len(amb), 2)
        self.assertTrue(all(c.destinatarios == ra.DEST_AMBIENTAL for c in amb))
        self.assertTrue(all(c.evidencia == "Programado" for c in amb))
        self.assertTrue(all(c.hora == time(10, 0) for c in amb))
        self.assertNotIn("CO₂", " ".join(c.detalle for c in amb))

    def test_ambiental_solo_aplica_a_sanna(self):
        correos = self._cargar(
            date(2026, 10, 5), date(2026, 10, 5),
            limites=[self._limite_amb("Humedad", empresa="Oechsle")])
        self.assertEqual([c for c in correos if c.sistema == "Ambiental"], [])

    def test_seccion_modulo_por_empresa_y_dia(self):
        dias = [date(2026, 10, 5), date(2026, 10, 6)]
        correos = self._cargar(dias[0], dias[-1])
        html = ra.seccion_modulo(2, "energia", dias[0], dias[-1], dias,
                                 [], [], correos)
        self.assertIn("2. Energía", html)
        self.assertIn("Oechsle", html)
        self.assertIn("lun 05 oct", html)
        self.assertIn("mar 06 oct", html)
        self.assertIn("Correos, día por día", html)
        self.assertNotIn('class="tablon"', html)          # el detalle reemplaza al tablón

    def test_tablon_cuenta_correos_fuera_del_rango(self):
        # La alerta del último día puede reportarse al siguiente: se ve aparte.
        ev = _evento(_dt(8, 23, 30))
        ev.reportada_cuando = _dt(9, 0, 5)
        correos = self._cargar(date(2026, 10, 5), date(2026, 10, 8), [ev])
        html = ra.tablon_modulo("Sanna", "energia",
                                [date(2026, 10, 5) + timedelta(days=i)
                                 for i in range(4)], [], correos)
        self.assertIn("Otros días", html)

    def test_totales_y_tabla_por_tipo(self):
        correos = self._cargar(date(2026, 10, 5), date(2026, 10, 5),
                               eventos=[_evento(_dt(5, 10, 0))],
                               huecos=[_hueco(_dt(5, 8), _dt(5, 10))])
        tot = ra.totales_correos(correos)
        self.assertEqual(tot["Registrado"], 1)
        self.assertEqual(tot["Programado"], 4)           # 1 apertura + 1 cierre + 2 cobertura
        self.assertEqual(tot["Inferido"], 1)
        self.assertEqual(tot["total"], 6)
        filas = ra.correos_por_tipo(correos)
        self.assertEqual(filas[0]["evidencia"], "Registrado")   # registrados primero
        self.assertEqual(sum(f["n"] for f in filas), 6)

    def test_portada_y_seccion_de_correos(self):
        correos = self._cargar(date(2026, 10, 5), date(2026, 10, 5),
                               eventos=[_evento(_dt(5, 10, 0))])
        doc = ra.armar_html(date(2026, 10, 5), date(2026, 10, 5), [], [], [], _config(),
                            _comparativas(date(2026, 10, 5), date(2026, 10, 5)),
                            _dt(6, 7), ra.CORTE_MIN, correos=correos)
        self.assertIn("Correos del periodo", doc)
        self.assertIn("Alertas vs correos por módulo", doc)
        self.assertIn("Correos, día por día", doc)
        self.assertIn("12:05", doc)
        self.assertIn("Estado actual (apertura)", doc)

    def test_detalle_correos_en_cada_empresa(self):
        correos = self._cargar(date(2026, 10, 5), date(2026, 10, 6))
        doc = ra.armar_html(date(2026, 10, 5), date(2026, 10, 6), [], [], [], _config(),
                            _comparativas(date(2026, 10, 5), date(2026, 10, 6)),
                            _dt(8, 7), ra.CORTE_MIN, correos=correos)
        self.assertIn("Correos, día por día", doc)
        self.assertNotIn("B · Correos", doc)              # ya no es un anexo global
        self.assertIn("lun 05 oct", doc)
        self.assertIn("mar 06 oct", doc)

    def test_filtro_por_empresa_en_correos(self):
        correos = self._cargar(date(2026, 10, 5), date(2026, 10, 5),
                               eventos=[_evento(_dt(5, 10, 0), empresa="Sanna")])
        doc = ra.armar_html(date(2026, 10, 5), date(2026, 10, 5), [], [], [], _config(),
                            _comparativas(date(2026, 10, 5), date(2026, 10, 5)),
                            _dt(6, 7), ra.CORTE_MIN, empresas=["Scotiabank"],
                            correos=correos)
        self.assertNotIn("Estado actual (apertura)", doc)   # es de Oechsle
        self.assertNotIn("Llave general TG-TR1", doc)


if __name__ == "__main__":
    unittest.main()

