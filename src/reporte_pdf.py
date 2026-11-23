"""Utilidades compartidas para imprimir reportes HTML a PDF con Chrome/Edge.

Se extrajeron de `scripts/reporte_conectividad.py` para que los reportes nuevos
no dupliquen la detección del navegador ni el manejo de Chrome headless (que en
macOS a veces no termina el proceso aunque el PDF ya esté escrito).

Uso típico:

    from src.reporte_pdf import buscar_chrome, imprimir_pdf
    chrome = buscar_chrome()
    imprimir_pdf(html, Path("reporte.pdf"), chrome)
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def buscar_chrome(preferido: str | None = None) -> str:
    """Primera ruta existente entre la indicada, CHROME_PATH y los candidatos."""
    candidatos = [preferido, os.environ.get("CHROME_PATH"), *CHROME_CANDIDATOS]
    candidatos += [shutil.which(n) for n in ("google-chrome", "google-chrome-stable",
                                             "chromium", "chromium-browser", "chrome",
                                             "msedge")]
    for c in candidatos:
        if c and Path(c).exists():
            return c
    raise SystemExit("No encontré Chrome/Edge para imprimir el PDF. Indica la ruta con "
                     "--chrome o la variable CHROME_PATH (usa --html para obtener solo el HTML).")


def _pdf_listo(pdf: Path, tam_previo: int) -> tuple[bool, int]:
    """True cuando el PDF existe, termina en %%EOF y ya no crece."""
    try:
        tam = pdf.stat().st_size
        if tam == 0:
            return False, 0
        with open(pdf, "rb") as fh:
            fh.seek(max(0, tam - 1024))
            cola = fh.read()
    except OSError:
        return False, 0
    return (tam == tam_previo and cola.rstrip().endswith(b"%%EOF")), tam


def _cerrar(proc: subprocess.Popen) -> None:
    """Cierra Chrome y sus procesos hijos (renderer, GPU...)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def imprimir_pdf(html_txt: str, destino: Path, chrome: str, timeout: float = 180) -> None:
    """Imprime el HTML a PDF con Chrome headless.

    En macOS Chrome headless escribe el PDF en segundos pero a veces no termina
    el proceso: se espera a que el archivo esté completo y luego se cierra Chrome.
    """
    # mkdtemp + rmtree(ignore_errors): algún proceso hijo de Chrome puede
    # seguir soltando archivos del perfil un instante después de cerrarlo.
    tmp_dir = Path(tempfile.mkdtemp(prefix="zeia-reporte-"))
    try:
        src = tmp_dir / "reporte.html"
        pdf = tmp_dir / "reporte.pdf"
        log = tmp_dir / "chrome.log"
        src.write_text(html_txt, encoding="utf-8")
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
               "--no-default-browser-check", "--disable-extensions",
               "--disable-background-networking", "--disable-component-update",
               f"--user-data-dir={tmp_dir / 'perfil'}",
               "--no-pdf-header-footer", f"--print-to-pdf={pdf}", src.as_uri()]
        listo, tam = False, -1
        with open(log, "wb") as flog:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=flog,
                                    start_new_session=(os.name != "nt"))
            try:
                limite = time.monotonic() + timeout
                while time.monotonic() < limite:
                    time.sleep(0.3)
                    listo, tam = _pdf_listo(pdf, tam)
                    if listo or (proc.poll() is not None and not pdf.exists()):
                        break
            finally:
                _cerrar(proc)
        if not listo:
            cola = log.read_text(encoding="utf-8", errors="replace")[-1500:]
            raise SystemExit(f"Chrome no generó el PDF en {timeout:.0f} s:\n{cola}")
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf, destino)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
