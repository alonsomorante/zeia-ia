"""Pruebas del paquete del bot para el servidor (deploy/bot/empaquetar.py).

    python -m unittest tests.test_deploy_bot -v
"""
from __future__ import annotations

import ast
import importlib.util
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUTA = ROOT / "deploy" / "bot" / "empaquetar.py"
_spec = importlib.util.spec_from_file_location("empaquetar", RUTA)
emp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emp)


def _imports_propios(ruta: Path) -> set[str]:
    """Módulos de src/ que importa un archivo (from src import x / from .x import)."""
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.ImportFrom):
            if n.module == "src" or (n.level == 1 and n.module is None):
                out.update(f"src/{a.name}.py" for a in n.names)
            elif n.module and n.module.startswith("src."):
                out.add(f"src/{n.module.split('.')[1]}.py")
            elif n.level == 1 and n.module:
                out.add(f"src/{n.module.split('.')[0]}.py")
    return out


class TestLista(unittest.TestCase):
    def test_trae_todo_lo_que_el_bot_importa(self):
        pendientes = ["scripts/telegram_bot.py", "scripts/avisos.py", "deploy/bot/verificar.py"]
        vistos = set()
        while pendientes:
            a = pendientes.pop()
            if a in vistos:
                continue
            vistos.add(a)
            pendientes += [m for m in _imports_propios(ROOT / a) if (ROOT / m).is_file()]
        faltan = sorted(m for m in vistos if m not in emp.ARCHIVOS)
        self.assertEqual(faltan, [])

    def test_no_trae_el_chat_ni_la_webapp_ni_secretos(self):
        for fuera in ("webapp.py", "cli.py", "src/agent.py", "src/prompts.py", "src/tools.py",
                      "src/elevenlabs.py", "src/speech.py", "src/pdf_tools.py", ".env"):
            self.assertNotIn(fuera, emp.ARCHIVOS)
        self.assertFalse([a for a in emp.ARCHIVOS if a.endswith((".pem", ".pdf", ".env"))])

    def test_prohibidos_aunque_alguien_los_liste(self):
        for malo in ([".env"], ["energy.pem"], ["enero-2025.pdf"]):
            with self.assertRaises(SystemExit):
                emp._validar_lista(malo)


class TestPaquete(unittest.TestCase):
    def test_contenido_en_lf_con_version_y_sh_ejecutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            destino = emp.empaquetar(Path(tmp) / "bot.tar.gz", ver="abc123")
            with tarfile.open(destino) as tar:
                nombres = tar.getnames()
                self.assertEqual(sorted(nombres), sorted(emp.ARCHIVOS + ["VERSION"]))
                for m in tar.getmembers():
                    datos = tar.extractfile(m).read()
                    self.assertNotIn(b"\r\n", datos, m.name)
                    self.assertEqual(m.uid, 0)
                sh = tar.getmember("deploy/bot/instalar.sh")
                self.assertEqual(sh.mode, 0o755)
                self.assertTrue(tar.extractfile("VERSION").read().startswith(b"abc123 "))


class TestEnv(unittest.TestCase):
    EJEMPLO = ("ENERGIA_DB_HOST=127.0.0.1\nENERGIA_DB_PORT=55432\nUSE_SSH_TUNNEL_ENERGIA=true\n"
               "ENERGIA_DB_PASSWORD=\nAMBIENTAL_DB_PASSWORD=\n"
               "TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\nAVISOS_MODO=simular\n")

    def _armar(self, propio: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            ej, pr = Path(tmp) / "ejemplo", Path(tmp) / ".env"
            ej.write_text(self.EJEMPLO, encoding="utf-8")
            pr.write_text(propio, encoding="utf-8")
            return emp.armar_env(pr, ej)

    def test_solo_variables_del_bot_y_bases_siempre_por_tunel(self):
        txt = self._armar("OPENROUTER_API_KEY=sk-secreta\nELEVENLABS_API_KEY=otra\n"
                          "ENERGIA_DB_PORT=5432\nUSE_SSH_TUNNEL_ENERGIA=false\n"
                          "ENERGIA_DB_PASSWORD=p1\nAMBIENTAL_DB_PASSWORD=p2.\n"
                          "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=-55   # grupo\n"
                          "AVISOS_MODO=enviar\n")
        self.assertNotIn("OPENROUTER", txt)
        self.assertNotIn("ELEVENLABS", txt)
        self.assertIn("ENERGIA_DB_PORT=55432\n", txt)          # el de la laptop no cuenta
        self.assertIn("USE_SSH_TUNNEL_ENERGIA=true\n", txt)
        self.assertIn("AMBIENTAL_DB_PASSWORD=p2.\n", txt)     # el punto final se conserva
        self.assertIn("TELEGRAM_CHAT_ID=-55\n", txt)          # sin el comentario
        self.assertIn("AVISOS_MODO=enviar\n", txt)

    def test_falla_si_falta_un_secreto(self):
        with self.assertRaises(SystemExit):
            self._armar("ENERGIA_DB_PASSWORD=p1\nTELEGRAM_BOT_TOKEN=1:a\nTELEGRAM_CHAT_ID=-5\n")


if __name__ == "__main__":
    unittest.main()
