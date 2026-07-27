"""
upload/upload_worker.py
Sube a la API en la nube los eventos confirmados que están pendientes en el
buffer local (SQLite).

Corre en su propio thread. Cada `poll_interval_seconds` revisa
local_buffer.get_pending() y hace POST a `{api_url}/events` por cada uno. Si
falla (sin conexión, timeout, error 5xx) deja el evento como pendiente y lo
reintenta en el próximo ciclo — nunca lo descarta.

La URL de la API y el token de autenticación del edge salen de variables de
entorno (se leen de un archivo .env vía python-dotenv, cargado en main.py):
    FISGON_API_URL     — URL base de la API, ej. https://.../
    FISGON_EDGE_TOKEN   — token de este edge, va en el header X-Edge-Token

Si FISGON_API_URL no está seteada, se usa api.base_url de cameras.yaml como
respaldo (útil para pruebas locales sin .env).

Uso típico (ver main.py):
    from upload.upload_worker import UploadWorker
    worker = UploadWorker(local_buffer=buffer, config=config)
    worker.start()
    ...
    worker.stop()
"""

import logging
import os
import threading
import time

import requests

logger = logging.getLogger("upload_worker")

DEFAULT_POLL_INTERVAL = 10  # segundos entre revisiones del buffer
DEFAULT_TIMEOUT = 10  # segundos de timeout por request

# Limpieza de eventos.db: sin esto, corriendo 24/7, la base local crece sin
# límite (los eventos ya subidos son historial, la nube es la fuente de
# verdad — ver CONTEXT.md). Se corre una vez por día, no en cada poll.
LIMPIEZA_INTERVALO_SEGUNDOS = 24 * 60 * 60
LIMPIEZA_RETENCION_DIAS = 7


def _asegurar_https(url: str) -> str:
    """Cloud Run (y la mayoría de las APIs en la nube) redirigen http a https
    con un 302, pero la librería requests baja el método POST a GET al
    seguir ese redirect (comportamiento heredado de compatibilidad con
    navegadores) — eso hacía que los eventos se perdieran silenciosamente
    contra un GET /events que no existe (404), en vez de subirse. Se fuerza
    https salvo que sea localhost, para no volver a pisar este bug si
    alguien pega una URL con http:// en el .env o en cameras.yaml."""
    if url.startswith("http://") and "localhost" not in url and "127.0.0.1" not in url:
        return "https://" + url[len("http://"):]
    return url


class UploadWorker:
    def __init__(self, local_buffer, config: dict, poll_interval_seconds: int = DEFAULT_POLL_INTERVAL):
        self.local_buffer = local_buffer
        base_url = os.environ.get("FISGON_API_URL") or config.get("api", {}).get("base_url", "")
        self.base_url = _asegurar_https(base_url.rstrip("/"))
        self.edge_token = os.environ.get("FISGON_EDGE_TOKEN")
        self.poll_interval = poll_interval_seconds

        if not self.base_url:
            logger.warning("FISGON_API_URL / api.base_url no configurado — upload_worker no podrá subir eventos")
        if not self.edge_token:
            logger.warning("FISGON_EDGE_TOKEN no configurado — se subirá sin autenticación")

        self._thread = None
        self._stop_event = threading.Event()
        self._ultima_limpieza = 0.0

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="upload-worker")
        self._thread.start()
        return self

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self):
        logger.info("Worker de subida iniciado")
        while not self._stop_event.is_set():
            try:
                self._subir_pendientes()
            except Exception:
                logger.exception("Error revisando/subiendo eventos pendientes")
            try:
                self._limpiar_si_corresponde()
            except Exception:
                logger.exception("Error limpiando eventos.db")
            self._stop_event.wait(self.poll_interval)
        logger.info("Worker de subida detenido")

    def _limpiar_si_corresponde(self):
        ahora = time.time()
        if ahora - self._ultima_limpieza < LIMPIEZA_INTERVALO_SEGUNDOS:
            return
        self._ultima_limpieza = ahora
        borrados = self.local_buffer.limpiar_antiguos(LIMPIEZA_RETENCION_DIAS)
        if borrados:
            logger.info(f"Limpieza local: {borrados} evento(s) subido(s) con más de "
                        f"{LIMPIEZA_RETENCION_DIAS} días borrados de eventos.db")

    def _subir_pendientes(self):
        if not self.base_url:
            return

        pendientes = self.local_buffer.get_pending()
        if not pendientes:
            return

        logger.info(f"Subiendo {len(pendientes)} evento(s) pendiente(s)")
        for evento in pendientes:
            if self._subir_evento(evento):
                self.local_buffer.mark_uploaded(evento["id"])

    def _subir_evento(self, evento: dict) -> bool:
        headers = {"X-Edge-Token": self.edge_token} if self.edge_token else {}
        # Contrato confirmado contra la API real: solo estos campos, "bbox"
        # como {x, y, width, height} (ya viene así de event_validator), y sin
        # "timestamp" (la API asigna el suyo al recibir el evento).
        payload = {
            "camera_id": evento["camera_id"],
            "object_class": evento["object_class"],
            "confidence": evento["confidence"],
            "bbox": evento["bbox"],
            "frames_validated": evento.get("frames_validated", 1),
        }
        if evento.get("snapshot_base64"):
            payload["snapshot_base64"] = evento["snapshot_base64"]

        try:
            resp = requests.post(
                f"{self.base_url}/events",
                json=payload,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.warning(f"[{evento['camera_id']}] No se pudo subir evento #{evento['id']}: {e}")
            return False

        if 200 <= resp.status_code < 300:
            logger.info(f"[{evento['camera_id']}] Evento #{evento['id']} subido")
            return True

        logger.warning(
            f"[{evento['camera_id']}] Evento #{evento['id']} rechazado por la API "
            f"({resp.status_code}): {resp.text[:200]}"
        )
        return False


if __name__ == "__main__":
    # Prueba aislada: crea un buffer temporal con un evento falso e intenta subirlo.
    # Uso: python upload/upload_worker.py [base_url]
    # (fallará si no se pasa base_url o la API no responde — es el comportamiento esperado)
    import sys
    import time
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))

    from events.local_buffer import LocalBuffer

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    buffer = LocalBuffer("test_upload.db")
    buffer.save_pending({
        "camera_id": "cam_test",
        "object_class": "pistol",
        "confidence": 0.95,
        "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
        "frames_validated": 3,
        "snapshot_base64": None,
        "timestamp": "2026-01-01T00:00:00+00:00",
    })

    config_test = {"api": {"base_url": sys.argv[1] if len(sys.argv) > 1 else ""}}
    worker = UploadWorker(buffer, config_test, poll_interval_seconds=3)
    worker.start()
    time.sleep(5)
    worker.stop()

    print(f"Pendientes tras el intento: {buffer.get_pending()}")
    buffer.close()
