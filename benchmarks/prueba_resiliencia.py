"""
benchmarks/prueba_resiliencia.py
Prueba de integración y resiliencia del camino edge → API ante cortes de red
(bloque F del capítulo de pruebas).

Qué se usa de verdad y qué se simula:

- La API es la real (código de API_Fisgon), levantada en esta PC con uvicorn,
  una base SQLite temporal y sin Telegram. No se toca producción: los eventos
  de prueba no llegan al panel del cliente ni disparan alertas reales.
- Del edge se usan las piezas reales que intervienen: LocalBuffer (SQLite) y
  UploadWorker, con su configuración de producción (timeout de 10 s,
  revisión periódica de 10 s, envío inmediato).
- El corte de red se simula con un intermediario TCP entre los dos, que se
  puede cortar de tres maneras:
    rechazar          conexión rechazada: como un cable desenchufado o el
                      router apagado (el sistema operativo avisa enseguida).
    colgar            se acepta la conexión y nunca se responde: como una red
                      que se cae sin avisar (el cliente espera su timeout).
    perder_respuesta  el pedido llega a la API y se procesa, pero la
                      respuesta no vuelve: reproduce el duplicado observado
                      en producción (bitácora, hallazgo 8).

No se corta la red de la PC porque la sesión desde la que se corre la prueba
depende de internet; el intermediario corta solo el enlace edge → API.

Escenarios:
    1. Corte con conexión rechazada: eventos durante el corte, se restablece.
    2. Corte silencioso (colgar): idem.
    3. Caída del proceso del edge durante un corte, con eventos pendientes:
       se mata el proceso a la fuerza, se restablece la red y se arranca uno
       nuevo sobre la misma base local.
    4. Respuesta perdida: el evento se crea en la API pero el edge no se
       entera y reintenta.

Para cada escenario verifica: ningún evento perdido, cuántos eventos quedaron
en la API (duplicados) y cuánto tardó cada evento en subir tras restablecerse
el enlace.

Uso:
    python benchmarks/prueba_resiliencia.py --salida benchmarks/resultados/f_resiliencia
"""

import argparse
import hashlib
import json
import multiprocessing
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).parent.parent
API_DIR = RAIZ.parent / "API_Fisgon"
API_PYTHON = API_DIR / ".venv" / "Scripts" / "python.exe"
sys.path.insert(0, str(RAIZ))

import requests  # noqa: E402

from events.local_buffer import LocalBuffer  # noqa: E402
from upload import upload_worker as modulo_upload  # noqa: E402
from upload.upload_worker import UploadWorker  # noqa: E402

PUERTO_API = 8801
PUERTO_PROXY = 8802
TOKEN = "token-edge-prueba-resiliencia"
CAMARA = "cam_prueba"


# --- Intermediario TCP con corte controlable ---------------------------------

class Intermediario:
    def __init__(self, puerto_escucha: int, puerto_destino: int):
        self.puerto_escucha = puerto_escucha
        self.puerto_destino = puerto_destino
        self.modo = "normal"
        self._servidor = None
        self._activo = True

    def iniciar(self):
        self._servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._servidor.bind(("127.0.0.1", self.puerto_escucha))
        self._servidor.listen(50)
        threading.Thread(target=self._aceptar, daemon=True).start()

    def _aceptar(self):
        while self._activo:
            try:
                cliente, _ = self._servidor.accept()
            except OSError:
                return
            threading.Thread(target=self._atender, args=(cliente,), daemon=True).start()

    def _atender(self, cliente):
        modo = self.modo
        if modo == "rechazar":
            # Cierre abrupto (RST): el cliente ve un error de conexión al instante.
            cliente.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            cliente.close()
            return
        if modo == "colgar":
            # Se acepta y no se responde nunca; se cierra recién al volver la red.
            while self._activo and self.modo == "colgar":
                time.sleep(0.2)
            cliente.close()
            return
        destino = socket.create_connection(("127.0.0.1", self.puerto_destino))
        perder = modo == "perder_respuesta"
        hilos = [
            threading.Thread(target=self._copiar, args=(cliente, destino, False), daemon=True),
            threading.Thread(target=self._copiar, args=(destino, cliente, perder), daemon=True),
        ]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        for s in (cliente, destino):
            try:
                s.close()
            except OSError:
                pass

    @staticmethod
    def _copiar(origen, destino, descartar: bool):
        try:
            while True:
                datos = origen.recv(65536)
                if not datos:
                    break
                if not descartar:
                    destino.sendall(datos)
        except OSError:
            pass
        finally:
            try:
                destino.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def detener(self):
        self._activo = False
        try:
            self._servidor.close()
        except OSError:
            pass


# --- API local ---------------------------------------------------------------

def levantar_api(directorio: Path) -> subprocess.Popen:
    """Arranca la API real con una base temporal y un edge de prueba dado de alta."""
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{(directorio / 'api.db').as_posix()}",
        "SNAPSHOTS_DIR": str(directorio / "snapshots"),
        "EVIDENCE_FACE_BLUR_ENABLED": "false",
        "TELEGRAM_BOT_TOKEN": "",
        "API_KEY": "",
        "PYTHONIOENCODING": "utf-8",
    }
    alta = f"""
import hashlib
from API_Fisgon import database, user_model, edge_model
database.init_db()
from sqlalchemy.orm import Session
with Session(database.engine) as db:
    u = user_model.User(full_name="Prueba", business_name="Prueba resiliencia",
                        email="resiliencia@example.com", password_hash="x", is_admin=False, is_active=True)
    db.add(u); db.flush()
    db.add(edge_model.EdgeDevice(user_id=u.id, name="edge-prueba", max_cameras=4,
                                 token_hash=hashlib.sha256({TOKEN!r}.encode()).hexdigest()))
    db.commit()
print("alta ok")
"""
    subprocess.run([str(API_PYTHON), "-c", alta], cwd=str(API_DIR.parent), env=env, check=True,
                   capture_output=True)
    proceso = subprocess.Popen(
        [str(API_PYTHON), "-m", "uvicorn", "API_Fisgon.main:app", "--port", str(PUERTO_API), "--log-level", "warning"],
        cwd=str(API_DIR.parent), env=env, stdout=subprocess.DEVNULL, stderr=open(directorio / "api.log", "w"),
    )
    limite = time.monotonic() + 60
    while time.monotonic() < limite:
        try:
            if requests.get(f"http://127.0.0.1:{PUERTO_API}/health", timeout=1).status_code == 200:
                return proceso
        except requests.RequestException:
            time.sleep(0.5)
    proceso.kill()
    sys.exit("La API local no arrancó (ver api.log)")


def eventos_en_api(directorio: Path) -> int:
    with sqlite3.connect(directorio / "api.db") as c:
        return c.execute("SELECT COUNT(*) FROM events").fetchone()[0]


# --- Edge ----------------------------------------------------------------------

def evento(i: int, escenario: str) -> dict:
    return {
        "camera_id": CAMARA,
        "object_class": "pistol",
        "confidence": 0.9,
        "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
        "frames_validated": 2,
        "snapshot_base64": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def configurar_entorno_edge(directorio: Path):
    os.environ["FISGON_API_URL"] = f"http://127.0.0.1:{PUERTO_PROXY}"
    os.environ["FISGON_EDGE_TOKEN"] = TOKEN
    modulo_upload.LATENCIA_CSV = directorio / "latencia.csv"  # no tocar logs/ de producción


def esperar_subidos(buffer: LocalBuffer, segundos: float) -> bool:
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if not buffer.get_pending():
            return True
        time.sleep(0.2)
    return False


def tiempos_de_subida(buffer: LocalBuffer, ids: list, t_restablecido: float, timeout: float) -> list:
    """Segundos desde que vuelve el enlace hasta que cada evento queda subido."""
    pendientes = set(ids)
    tiempos = {}
    limite = time.monotonic() + timeout
    while pendientes and time.monotonic() < limite:
        aun = {e["id"] for e in buffer.get_pending()}
        for i in list(pendientes):
            if i not in aun:
                tiempos[i] = round(time.monotonic() - t_restablecido, 2)
                pendientes.discard(i)
        time.sleep(0.1)
    return [tiempos.get(i) for i in ids]


def escenario_corte(nombre, modo, intermediario, directorio, n_eventos=3, duracion_corte=25):
    print(f"\n== {nombre} (modo {modo})")
    base = directorio / f"edge_{modo}.db"
    buffer = LocalBuffer(base)
    worker = UploadWorker(buffer, config={}).start()
    antes = eventos_en_api(directorio)

    intermediario.modo = modo
    t_corte = time.monotonic()
    ids = []
    for i in range(n_eventos):
        ids.append(buffer.save_pending(evento(i, nombre)))
        worker.nuevo_evento(ids[-1])
        time.sleep(duracion_corte / (n_eventos + 1))
    pendientes_durante = len(buffer.get_pending())
    time.sleep(max(0, duracion_corte - (time.monotonic() - t_corte)))

    intermediario.modo = "normal"
    t_restablecido = time.monotonic()
    print(f"  corte de {t_restablecido - t_corte:.1f} s, {pendientes_durante} pendientes al final del corte")
    tiempos = tiempos_de_subida(buffer, ids, t_restablecido, timeout=40)
    worker.stop()
    perdidos = len(buffer.get_pending())
    buffer.close()
    creados = eventos_en_api(directorio) - antes
    print(f"  subidos tras restablecer en {tiempos} s; perdidos {perdidos}; creados en la API {creados}")
    return {
        "modo": modo,
        "duracion_corte_s": round(t_restablecido - t_corte, 1),
        "eventos_generados": n_eventos,
        "pendientes_al_final_del_corte": pendientes_durante,
        "segundos_hasta_subir_tras_restablecer": tiempos,
        "eventos_perdidos": perdidos,
        "eventos_creados_en_api": creados,
        "duplicados": creados - n_eventos,
    }


def _proceso_edge_que_muere(base: str, n_eventos: int, url: str, lista_ids):
    """Proceso hijo: un edge que guarda eventos durante el corte (y no puede
    subirlos) hasta que el padre lo mata a la fuerza."""
    os.environ["FISGON_API_URL"] = url
    os.environ["FISGON_EDGE_TOKEN"] = TOKEN
    buffer = LocalBuffer(base)
    worker = UploadWorker(buffer, config={}).start()
    for i in range(n_eventos):
        lista_ids.append(buffer.save_pending(evento(i, "caida")))
        worker.nuevo_evento(lista_ids[-1])
        time.sleep(1)
    time.sleep(3600)  # espera a que lo maten


def escenario_caida(intermediario, directorio, n_eventos=3):
    print("\n== Caída del proceso durante un corte")
    base = directorio / "edge_caida.db"
    antes = eventos_en_api(directorio)
    intermediario.modo = "rechazar"

    with multiprocessing.Manager() as gestor:
        ids = gestor.list()
        hijo = multiprocessing.Process(
            target=_proceso_edge_que_muere,
            args=(str(base), n_eventos, os.environ["FISGON_API_URL"], ids),
        )
        hijo.start()
        while len(ids) < n_eventos:
            time.sleep(0.2)
        time.sleep(2)
        hijo.kill()  # TerminateProcess: sin cierre ordenado, como un corte de luz
        hijo.join()
        ids = list(ids)
    print(f"  proceso del edge terminado a la fuerza (código {hijo.exitcode}) con {n_eventos} eventos sin subir")

    buffer = LocalBuffer(base)
    pendientes_tras_caida = len(buffer.get_pending())
    intermediario.modo = "normal"
    t_restablecido = time.monotonic()
    worker = UploadWorker(buffer, config={}).start()  # el "reinicio" del edge
    tiempos = tiempos_de_subida(buffer, ids, t_restablecido, timeout=40)
    worker.stop()
    perdidos = len(buffer.get_pending())
    buffer.close()
    creados = eventos_en_api(directorio) - antes
    print(f"  pendientes al reabrir: {pendientes_tras_caida}; subidos en {tiempos} s; "
          f"perdidos {perdidos}; creados en la API {creados}")
    return {
        "eventos_generados": n_eventos,
        "codigo_salida_proceso_muerto": hijo.exitcode,
        "pendientes_al_reabrir_la_base": pendientes_tras_caida,
        "segundos_hasta_subir_tras_reiniciar": tiempos,
        "eventos_perdidos": perdidos,
        "eventos_creados_en_api": creados,
        "duplicados": creados - n_eventos,
    }


def escenario_respuesta_perdida(intermediario, directorio):
    print("\n== Respuesta perdida (la API procesa, el edge no se entera)")
    base = directorio / "edge_respuesta_perdida.db"
    buffer = LocalBuffer(base)
    worker = UploadWorker(buffer, config={}).start()
    antes = eventos_en_api(directorio)

    intermediario.modo = "perder_respuesta"
    event_id = buffer.save_pending(evento(0, "respuesta_perdida"))
    worker.nuevo_evento(event_id)
    time.sleep(12)  # más que el timeout de 10 s: el primer intento expira
    creados_durante = eventos_en_api(directorio) - antes
    intermediario.modo = "normal"
    t_restablecido = time.monotonic()
    tiempos = tiempos_de_subida(buffer, [event_id], t_restablecido, timeout=40)
    worker.stop()
    buffer.close()
    creados = eventos_en_api(directorio) - antes
    print(f"  creados en la API mientras se perdían las respuestas: {creados_durante}; "
          f"total creados por 1 evento: {creados}")
    return {
        "eventos_generados": 1,
        "creados_en_api_con_la_respuesta_perdida": creados_durante,
        "segundos_hasta_subir_tras_restablecer": tiempos,
        "eventos_creados_en_api": creados,
        "duplicados": creados - 1,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--salida", required=True)
    args = parser.parse_args()

    directorio = Path(tempfile.mkdtemp(prefix="fisgon_resiliencia_"))
    print(f"Directorio temporal: {directorio}")
    api = levantar_api(directorio)
    intermediario = Intermediario(PUERTO_PROXY, PUERTO_API)
    intermediario.iniciar()
    configurar_entorno_edge(directorio)

    try:
        resultados = {
            "fecha": datetime.now().isoformat(timespec="seconds"),
            "configuracion_edge": {
                "timeout_post_s": modulo_upload.DEFAULT_TIMEOUT,
                "revision_periodica_s": modulo_upload.DEFAULT_POLL_INTERVAL,
                "envio_inmediato": True,
            },
            "corte_conexion_rechazada": escenario_corte("Corte con conexión rechazada", "rechazar",
                                                        intermediario, directorio),
            "corte_silencioso": escenario_corte("Corte silencioso", "colgar", intermediario, directorio),
            "caida_del_proceso": escenario_caida(intermediario, directorio),
            "respuesta_perdida": escenario_respuesta_perdida(intermediario, directorio),
        }
    finally:
        intermediario.detener()
        api.kill()

    salida = Path(args.salida).with_suffix(".json")
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(resultados, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResultados en {salida}")


if __name__ == "__main__":
    main()
