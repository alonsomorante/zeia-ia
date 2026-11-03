"""Pruebas de las fotos del día a horas fijas (sin red, base ni navegador).

    python -m unittest tests.test_fotos_dia -v
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

from src import fotos_dia as fd  # noqa: E402

LIMA = fd.LIMA
HOY = date(2026, 11, 3)


def a_las(hora: int, minuto: int = 0, dia: int = 3) -> datetime:
    return datetime(2026, 11, dia, hora, minuto, tzinfo=LIMA)


class Horas(unittest.TestCase):
    def test_horas_de_la_config(self):
        self.assertEqual(fd.horas_de("09:00,14:00,20:00"), [540, 840, 1200])
        self.assertEqual(fd.horas_de(" 20:00 , 9:00 "), [540, 1200])     # ordena
        self.assertEqual(fd.horas_de("09:00,ocho,25:00"), [540])         # ignora lo raro
        self.assertEqual(fd.horas_de("09:00,09:00"), [540])
        for apagado in ("apagado", "OFF", " no "):
            self.assertEqual(fd.horas_de(apagado), [], apagado)
        for raro in ("", "ocho", None):
            self.assertEqual(fd.horas_de(raro), [540, 840, 1200], raro)


class FotosDia(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ruta = Path(tmp.name) / "data" / "fotos_dia.json"
        self.t = a_las(9, 0)
        self.enviados, self.falla = [], {}

    def enviar(self, modulo, hora_min):
        if modulo in self.falla:
            raise self.falla[modulo]
        self.enviados.append((modulo, hora_min))

    def fotos(self, horas="09:00,14:00,20:00"):
        return fd.FotosDia(self.enviar, horas=horas, ruta=self.ruta,
                           ahora=lambda: self.t, log=lambda m: None)

    def test_a_cada_hora_manda_los_tres_modulos_una_sola_vez(self):
        f = self.fotos()
        self.assertEqual(f.revisar(), ["ambiental", "energia", "agua"])
        self.t = a_las(9, 30)
        self.assertEqual(f.revisar(), [])
        self.assertEqual(self.fotos().revisar(), [])          # reinicio del bot: no repite
        self.t = a_las(14, 0)
        self.assertEqual(len(f.revisar()), 3)
        self.t = a_las(20, 1)
        self.assertEqual(len(f.revisar()), 3)
        self.assertEqual([h for _, h in self.enviados], [540] * 3 + [840] * 3 + [1200] * 3)
        guardado = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(guardado["dia"], "2026-11-03")
        self.assertEqual(len(guardado["hechos"]), 9)

    def test_entre_horas_no_manda_nada(self):
        for h, m in ((8, 59), (10, 0), (13, 59), (21, 0), (23, 59), (0, 5)):
            self.t = a_las(h, m)
            self.assertEqual(self.fotos().revisar(), [], f"{h}:{m:02d}")
        self.assertEqual(self.enviados, [])

    def test_si_el_bot_vuelve_dentro_de_la_hora_la_manda_y_si_no_la_salta(self):
        self.t = a_las(9, 59)
        self.assertEqual(len(self.fotos().revisar()), 3)
        self.enviados.clear()
        self.t = a_las(15, 0)                                 # la de 14:00 ya no vale
        self.assertEqual(self.fotos().revisar(), [])
        self.assertEqual(self.enviados, [])

    def test_el_dia_siguiente_vuelve_a_empezar(self):
        f = self.fotos()
        f.revisar()
        self.t = a_las(9, 0, dia=4)
        self.assertEqual(len(f.revisar()), 3)
        self.assertEqual(json.loads(self.ruta.read_text(encoding="utf-8"))["dia"], "2026-11-04")

    def test_la_hora_es_la_de_lima_aunque_el_reloj_este_en_utc(self):
        self.t = a_las(20, 0).astimezone(timezone.utc)        # 01:00 del día 4 en UTC
        self.assertEqual(self.fotos().pendientes()[:2], (HOY, 1200))

    def test_un_modulo_que_falla_se_reintenta_mientras_dure_la_ventana(self):
        self.falla["energia"] = RuntimeError("túnel caído\ndetalle")
        f = self.fotos()
        self.assertEqual(f.revisar(), ["ambiental", "agua"])
        self.t += timedelta(minutes=fd.REINTENTO_MIN - 1)
        self.assertEqual(f.revisar(), [])                     # todavía no toca reintentar
        self.t += timedelta(minutes=1)
        self.assertEqual(f.revisar(), [])                     # sigue fallando
        del self.falla["energia"]
        self.t += timedelta(minutes=fd.REINTENTO_MIN)
        self.assertEqual(f.revisar(), ["energia"])
        self.assertEqual([m for m, _ in self.enviados], ["ambiental", "agua", "energia"])

    def test_si_falla_toda_la_ventana_se_deja(self):
        self.falla["agua"] = RuntimeError("la base no responde")
        f = self.fotos()
        while self.t < a_las(10, 0):
            f.revisar()
            self.t += timedelta(minutes=fd.REINTENTO_MIN)
        del self.falla["agua"]
        self.assertEqual(f.revisar(), [])
        self.assertEqual([m for m, _ in self.enviados], ["ambiental", "energia"])

    def test_si_no_puede_anotar_igual_no_repite(self):
        f = self.fotos()
        with mock.patch.object(f, "_guardar", side_effect=OSError("disco lleno")):
            self.assertEqual(len(f.revisar()), 3)
            self.assertEqual(f.revisar(), [])
        self.assertEqual(len(self.enviados), 3)

    def test_apagado_no_manda_nunca(self):
        f = self.fotos(horas="apagado")
        self.assertEqual(f.revisar(), [])
        self.assertEqual(self.enviados, [])
        self.assertIn("apagadas", f.descripcion())

    def test_un_error_inesperado_no_tumba_el_bot(self):
        f = fd.FotosDia(self.enviar, horas="09:00", ruta=self.ruta,
                        ahora=lambda: 1 / 0, log=lambda m: None)
        self.assertEqual(f.revisar(), [])


if __name__ == "__main__":
    unittest.main()
