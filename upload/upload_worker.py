"""
upload/upload_worker.py
Sube a la API en la nube los eventos confirmados que están pendientes en el
buffer local (SQLite).

Corre en su propio thread. Cuando event_validator confirma un evento, main.py
llama a nuevo_evento() y el worker se despierta en el acto para subirlo. Además,
cada `poll_interval_seconds` revisa local_buffer.get_pending() por su cuenta:
esa revisión periódica es el reintento de lo que no se pudo subir (sin conexión,
timeout, error 5xx) — un evento nunca se descarta.

Antes la revisión periódica era el ÚNICO disparador: un evento confirmado
esperaba en SQLite entre 0 y 10 s hasta el próximo ciclo, lo que por sí solo
hacía imposible alertar en menos de 3 s (RNF001). Ver SISTEMA.md sección 5.11.
La opción upload.envio_inmediato de cameras.yaml (true por defecto) existe para
poder medir el comportamiento anterior; no hay motivo para apagarla en producción.

Los clips de video se suben en un hilo aparte. Antes compartían el hilo con los
eventos y un clip de ~4 MB tardaba ~14 s en subir: una alerta que se confirmaba
mientras tanto esperaba detrás del video (medido: 28 s en total para un evento).
La alerta es lo urgente; el clip puede llegar después. Ver SISTEMA.md sección 5.12.

Por cada evento subido se registra la latencia de alerta, etapa por etapa, en
logs/edge.log y en logs/latencia_eventos.csv (ver _registrar_latencia).

La URL de la API y el token de autenticación del edge salen de variables de
entorno (.env vía python-dotenv, cargado en main.py):
    FISGON_API_URL      — URL base de la API, ej. https://.../
    FISGON_EDGE_TOKEN   — token de este edge, va en el header X-Edge-Token

Son credenciales, así que .env es el único lugar donde viven: cameras.yaml está
trackeado en git y terminarían commiteadas. La interfaz web edita ese mismo .env
(sección "API en la nube"), no hace falta abrirlo a mano.

api.base_url de cameras.yaml se sigue leyendo como respaldo, solo para no romper
instalaciones viejas que lo tengan cargado ahí; el token nunca se lee del YAML.

Uso típico (ver main.py):
    from upload.upload_worker import UploadWorker
    worker = UploadWorker(local_buffer=buffer, config=config)
    worker.start()
    ...
    worker.stop()
"""

import csv
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger("upload_worker")

DEFAULT_POLL_INTERVAL = 10  # segundos entre revisiones del buffer
DEFAULT_TIMEOUT = 10  # segundos de timeout por request

# Limpieza de eventos.db: sin esto, corriendo 24/7, la base local crece sin
# límite (los eventos ya subidos son historial, la nube es la fuente de
# verdad — ver CONTEXT.md). Se corre una vez por día, no en cada poll.
LIMPIEZA_INTERVALO_SEGUNDOS = 24 * 60 * 60
LIMPIEZA_RETENCION_DIAS = 7

LATENCIA_CSV = Path(__file__).parent.parent / "logs" / "latencia_eventos.csv"

# Etapas de la latencia de alerta, en orden. Cada una es una marca de tiempo
# time.monotonic() del mismo proceso, así que las diferencias son exactas
# aunque el reloj de la PC esté mal.
ETAPAS = (
    ("captura_primera", "primer frame con el arma detectada"),
    ("captura_confirmante", "frame que completa la validación temporal"),
    ("inferencia", "fin de la inferencia de ese frame"),
    ("confirmacion", "evento confirmado por event_validator"),
    ("guardado", "evento guardado en eventos.db"),
    ("envio", "inicio del POST /events"),
    ("respuesta", "respuesta de la API"),
)


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
        base_url_env = os.environ.get("FISGON_API_URL", "").strip()
        base_url_yaml = str((config.get("api") or {}).get("base_url") or "").strip()

        self.base_url = _asegurar_https((base_url_env or base_url_yaml).rstrip("/"))
        self.edge_token = os.environ.get("FISGON_EDGE_TOKEN", "").strip()
        self.poll_interval = poll_interval_seconds

        # Se deja asentado de dónde salió la URL: si quedó una vieja en el YAML,
        # "lo cambié en la interfaz y sigue yendo a la otra" se responde mirando
        # el log en vez de adivinando.
        if self.base_url:
            origen = ".env" if base_url_env else "cameras.yaml (respaldo)"
            logger.info(f"API en {self.base_url} (configurada desde {origen})")
        else:
            logger.warning("FISGON_API_URL no configurado — upload_worker no podrá subir eventos")
        if self.edge_token:
            # Nunca el token en sí: los logs se comparten para depurar.
            logger.info("Token del edge cargado desde .env")
        else:
            logger.warning("FISGON_EDGE_TOKEN no configurado — se subirá sin autenticación")

        self.envio_inmediato = bool((config.get("upload") or {}).get("envio_inmediato", True))
        if not self.envio_inmediato:
            logger.warning("upload.envio_inmediato desactivado: los eventos esperan la revisión "
                           f"periódica (cada {self.poll_interval}s) — solo para medir")

        self._thread = None
        self._thread_videos = None
        self._stop_event = threading.Event()
        self._despertar = threading.Event()
        self._ultima_limpieza = 0.0
        # Marcas de tiempo por id local de evento, para medir la latencia de
        # alerta. Solo en memoria: un evento que sobrevive a un reinicio se
        # sube igual, pero su latencia ya no tiene sentido medirla.
        self._marcas = {}
        self._marcas_lock = threading.Lock()

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="upload-worker")
        self._thread_videos = threading.Thread(target=self._run_videos, daemon=True, name="upload-videos")
        self._thread.start()
        self._thread_videos.start()
        return self

    def stop(self):
        self._stop_event.set()
        self._despertar.set()
        for hilo in (self._thread, self._thread_videos):
            if hilo is not None:
                hilo.join(timeout=5)

    def nuevo_evento(self, event_id: int, marcas: dict | None = None):
        """Lo llama main.py apenas se guarda un evento confirmado: anota sus
        marcas de tiempo y despierta al worker para subirlo ya."""
        if marcas:
            with self._marcas_lock:
                self._marcas[event_id] = dict(marcas)
        if self.envio_inmediato:
            self._despertar.set()

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
            # Duerme hasta la próxima revisión periódica o hasta que llegue un
            # evento nuevo, lo que pase primero.
            self._despertar.wait(self.poll_interval)
            self._despertar.clear()
        logger.info("Worker de subida detenido")

    def _run_videos(self):
        """Sube los clips pendientes en su propio hilo, para que nunca demoren
        una alerta. Sin apuro: con la revisión periódica alcanza."""
        while not self._stop_event.is_set():
            try:
                self._subir_videos_pendientes()
            except Exception:
                logger.exception("Error revisando/subiendo videos pendientes")
            self._stop_event.wait(self.poll_interval)

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
            remote_event_id = self._subir_evento(evento)
            if remote_event_id is not None:
                self.local_buffer.mark_uploaded(evento["id"], remote_event_id)

    def _subir_evento(self, evento: dict) -> int | None:
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

        self._marcar(evento["id"], "envio")
        try:
            resp = requests.post(
                f"{self.base_url}/events",
                json=payload,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.warning(f"[{evento['camera_id']}] No se pudo subir evento #{evento['id']}: {e}")
            return None

        if 200 <= resp.status_code < 300:
            self._marcar(evento["id"], "respuesta")
            logger.info(f"[{evento['camera_id']}] Evento #{evento['id']} subido")
            try:
                remote_id = int(resp.json()["id"])
            except (KeyError, TypeError, ValueError):
                logger.error("La API creó el evento pero no devolvió un id válido")
                return None
            self._registrar_latencia(evento, remote_id)
            return remote_id

        logger.warning(
            f"[{evento['camera_id']}] Evento #{evento['id']} rechazado por la API "
            f"({resp.status_code}): {resp.text[:200]}"
        )
        return None

    def _marcar(self, event_id: int, etapa: str):
        with self._marcas_lock:
            if event_id in self._marcas:
                self._marcas[event_id][etapa] = time.monotonic()

    def _registrar_latencia(self, evento: dict, remote_id: int):
        """Deja asentado cuánto tardó cada etapa de este evento.

        Lo que el sistema no puede medir desde adentro queda afuera y se
        reporta aparte: antes de captura_primera, la demora de la cámara y
        del flujo RTSP; después de respuesta, el envío de la API a Telegram
        (corre en segundo plano en la API, después de responder). Para cruzar
        con la hora del mensaje de Telegram se guarda la hora UTC estimada de
        cada marca, derivada del reloj de la PC en el momento de la respuesta.
        """
        with self._marcas_lock:
            marcas = self._marcas.pop(evento["id"], None)
        if not marcas or any(marcas.get(etapa) is None for etapa, _ in ETAPAS):
            return

        ahora_mono, ahora_utc = time.monotonic(), datetime.now(timezone.utc)

        def a_utc(mono: float) -> str:
            return (ahora_utc - timedelta(seconds=ahora_mono - mono)).isoformat(timespec="milliseconds")

        nombres = [etapa for etapa, _ in ETAPAS]
        tramos_ms = {
            f"{a}_a_{b}_ms": round((marcas[b] - marcas[a]) * 1000)
            for a, b in zip(nombres, nombres[1:])
        }
        total_ms = round((marcas["respuesta"] - marcas["captura_primera"]) * 1000)

        fila = {
            "evento_local": evento["id"],
            "evento_remoto": remote_id,
            "camera_id": evento["camera_id"],
            "object_class": evento["object_class"],
            "confidence": round(evento["confidence"], 3),
            "frames_validated": evento.get("frames_validated"),
            "envio_inmediato": self.envio_inmediato,
            **tramos_ms,
            "total_ms": total_ms,
            "captura_primera_utc": a_utc(marcas["captura_primera"]),
            # El snapshot es el frame confirmante: si en la imagen se ve un
            # reloj, comparar esta hora con la que muestra da la demora de la
            # cámara (ver benchmarks/reloj_pc.py).
            "captura_confirmante_utc": a_utc(marcas["captura_confirmante"]),
            "respuesta_utc": a_utc(marcas["respuesta"]),
        }
        logger.info(
            f"[{evento['camera_id']}] Latencia evento #{evento['id']}: {total_ms} ms desde la primera "
            "detección hasta la respuesta de la API ("
            + ", ".join(f"{k[:-3]} {v} ms" for k, v in tramos_ms.items()) + ")"
        )
        try:
            nuevo = not LATENCIA_CSV.exists()
            LATENCIA_CSV.parent.mkdir(parents=True, exist_ok=True)
            with open(LATENCIA_CSV, "a", newline="", encoding="utf-8") as f:
                escritor = csv.DictWriter(f, fieldnames=list(fila))
                if nuevo:
                    escritor.writeheader()
                escritor.writerow(fila)
        except OSError:
            logger.exception("No se pudo escribir logs/latencia_eventos.csv")

    def _subir_videos_pendientes(self):
        for item in self.local_buffer.get_pending_videos():
            if self._subir_video(item):
                self.local_buffer.mark_video_uploaded(item["id"])

    def _subir_video(self, item: dict) -> bool:
        headers = {"X-Edge-Token": self.edge_token} if self.edge_token else {}
        try:
            resp = requests.post(
                f"{self.base_url}/events/{item['remote_event_id']}/video",
                json={"video_base64": item["video_base64"]},
                headers=headers,
                timeout=max(DEFAULT_TIMEOUT, 30),
            )
        except requests.RequestException as exc:
            logger.warning("[%s] No se pudo subir el video del evento #%s: %s", item["camera_id"], item["id"], exc)
            return False

        if 200 <= resp.status_code < 300:
            logger.info("[%s] Video del evento #%s subido", item["camera_id"], item["id"])
            return True
        logger.warning(
            "[%s] Video del evento #%s rechazado por la API (%s): %s",
            item["camera_id"], item["id"], resp.status_code, resp.text[:200],
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
