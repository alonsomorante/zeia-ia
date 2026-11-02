"""Pruebas de la foto de cierre del día (sin red, base ni navegador).

    python -m unittest tests.test_foto_cierre -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import foto_cierre as fc  # noqa: E402

LIMA = fc.LIMA
AYER = date(2026, 10, 27)


def a_las(dia: int, hora: int, minuto: int = 0) -> datetime:
    return datetime(2026, 10, dia, hora, minuto, tzinfo=LIMA)


class Hora(unittest.TestCase):
    def test_hora_de_la_config(self):
        self.assertEqual(fc.hora_de("00:10"), 10)
        self.assertEqual(fc.hora_de("7:30"), 450)
        self.assertEqual(fc.hora_de(" 23:59 "), 1439)
        for apagado in ("apagado", "OFF", "no"):
            self.assertIsNone(fc.hora_de(apagado), apagado)
        for raro in ("", None, "24:00", "10", "ocho", "08:61", "1:2:3"):
            self.assertEqual(fc.hora_de(raro), fc.HORA_DEFECTO, raro)

    def test_dia_que_toca_segun_la_hora(self):
        self.assertEqual(fc.dia_debido(a_las(28, 0, 9), 10), date(2026, 10, 26))
        self.assertEqual(fc.dia_debido(a_las(28, 0, 10), 10), AYER)
        self.assertEqual(fc.dia_debido(a_las(28, 15, 0), 10), AYER)
        # 21:00 en Lima ya es el 29 en UTC: cuenta la fecha de Lima
        self.assertEqual(fc.dia_debido(a_las(28, 21, 0).astimezone(timezone.utc), 10), AYER)


class Cierre(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ruta = Path(tmp.name) / "data" / "foto_cierre.json"
        self.t = a_las(28, 0, 10)
        self.enviados, self.avisos, self.falla = [], [], {}

    def enviar(self, modulo, dia):
        if modulo in self.falla:
            raise self.falla[modulo]
        self.enviados.append((modulo, dia))

    def cierre(self, hora="00:10"):
        return fc.Cierre(self.enviar, self.avisos.append, hora=hora, ruta=self.ruta,
                         ahora=lambda: self.t, log=lambda m: None)

    def test_a_la_hora_manda_los_tres_modulos_del_dia_anterior_una_sola_vez(self):
        c = self.cierre()
        self.assertEqual(c.revisar(), ["ambiental", "energia", "agua"])
        self.assertEqual(self.enviados, [(m, AYER) for m in fc.MODULOS])
        self.t = a_las(28, 9, 0)
        self.assertEqual(c.revisar(), [])
        self.assertEqual(self.cierre().revisar(), [])        # reinicio del bot: no repite
        self.assertEqual(len(self.enviados), 3)
        self.assertEqual(json.loads(self.ruta.read_text(encoding="utf-8")),
                         {"dia": "2026-10-27", "hechos": ["ambiental", "energia", "agua"]})

    def test_antes_de_la_hora_no_manda_el_dia_que_acaba_de_terminar(self):
        self.t = a_las(28, 0, 9)
        c = self.cierre()
        c._guardar(date(2026, 10, 26), list(fc.MODULOS))     # el 26 salió ayer a las 00:10
        self.assertEqual(c.revisar(), [])
        self.t = a_las(28, 0, 10)
        self.assertEqual(len(c.revisar()), 3)
        self.assertEqual({d for _, d in self.enviados}, {AYER})

    def test_si_la_pc_estaba_apagada_manda_solo_el_ultimo_dia_al_volver(self):
        c = self.cierre()
        c._guardar(date(2026, 10, 24), list(fc.MODULOS))
        self.t = a_las(28, 8, 55)
        self.assertEqual(len(c.revisar()), 3)
        self.assertEqual({d for _, d in self.enviados}, {AYER})

    def test_un_modulo_que_falla_se_reintenta_sin_repetir_los_demas(self):
        self.falla["energia"] = RuntimeError("túnel caído\ndetalle")
        c = self.cierre()
        self.assertEqual(c.revisar(), ["ambiental", "agua"])
        self.t += timedelta(minutes=fc.REINTENTO_MIN - 1)
        self.assertEqual(c.revisar(), [])                    # todavía no toca reintentar
        del self.falla["energia"]
        self.t += timedelta(minutes=1)
        self.assertEqual(c.revisar(), ["energia"])
        self.assertEqual([m for m, _ in self.enviados], ["ambiental", "agua", "energia"])
        self.assertEqual(self.avisos, [])

    def test_tras_varios_fallos_avisa_en_el_grupo_y_deja_ese_dia(self):
        self.falla["agua"] = RuntimeError("la base no responde")
        c = self.cierre()
        for _ in range(fc.MAX_INTENTOS):
            c.revisar()
            self.t += timedelta(minutes=fc.REINTENTO_MIN)
        self.assertEqual(self.avisos, ["No pude armar la foto de cierre de /agua del "
                                       "martes 27 oct: la base no responde"])
        self.assertEqual(c.revisar(), [])
        self.assertEqual(len(self.avisos), 1)
        self.assertEqual(c.pendientes(), (None, []))
        self.assertEqual([m for m, _ in self.enviados], ["ambiental", "energia"])

    def test_si_tampoco_puede_avisar_sigue_reintentando(self):
        self.falla["agua"] = RuntimeError("sin red")

        def avisar(texto):
            raise RuntimeError("Telegram caído")

        c = fc.Cierre(self.enviar, avisar, hora="00:10", ruta=self.ruta,
                      ahora=lambda: self.t, log=lambda m: None)
        for _ in range(fc.MAX_INTENTOS + 1):
            c.revisar()
            self.t += timedelta(minutes=fc.REINTENTO_MIN)
        self.assertEqual(c.pendientes(), (AYER, ["agua"]))
        del self.falla["agua"]
        self.assertEqual(c.revisar(), ["agua"])

    def test_si_no_puede_anotar_igual_no_repite(self):
        c = self.cierre()
        with mock.patch.object(c, "_guardar", side_effect=OSError("disco lleno")):
            self.assertEqual(len(c.revisar()), 3)
            self.assertEqual(c.revisar(), [])
        self.assertEqual(len(self.enviados), 3)

    def test_apagado_no_manda_nunca(self):
        c = self.cierre(hora="apagado")
        self.assertEqual(c.revisar(), [])
        self.assertEqual(self.enviados, [])
        self.assertIn("apagada", c.descripcion())

    def test_un_error_inesperado_no_tumba_el_bot(self):
        c = fc.Cierre(self.enviar, self.avisos.append, hora="00:10", ruta=self.ruta,
                      ahora=lambda: 1 / 0, log=lambda m: None)
        self.assertEqual(c.revisar(), [])


if __name__ == "__main__":
    unittest.main()
