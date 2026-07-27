"""
camera_source.py
Captura de UNA fuente RTSP (cámara IP o canal de DVR/NVR) con:
  - Reconexión automática ante caídas del stream
  - Submuestreo fijo a INFERENCE_FPS (por defecto 5 fps) para la cola de inferencia
  - Filtro de movimiento opcional: evita mandar frames al modelo cuando la
    escena no cambia (ver motion_config)
  - Corre en su propio thread, entrega frames a una cola compartida

Uso típico (ver camera_manager.py para orquestar varias):

    from queue import Queue
    from camera_source import CameraSource

    cola = Queue(maxsize=200)
    cam = CameraSource(camera_id="cam_1", rtsp_url="rtsp://...", frame_queue=cola)
    cam.start()
    ...
    cam.stop()
"""

import cv2
import time
import threading
import logging

logger = logging.getLogger("camera_source")

# FPS fijo al que se envían frames a la cola de inferencia.
# No es el FPS de captura del stream (eso lo maneja la cámara/DVR),
# es cuántos de esos frames realmente se encolan para correr YOLOv8.
INFERENCE_FPS = 5

# Reintentos de reconexión: backoff simple (segundos entre intentos)
RECONNECT_DELAYS = [2, 5, 10, 30]  # el último valor se repite indefinidamente

# Filtro de movimiento desactivado por defecto (solo se activa si
# camera_manager.py pasa motion_config con enabled=True desde cameras.yaml).
DEFAULT_MOTION_CONFIG = {
    "enabled": False,
    "threshold": 25,
    "min_area_fraction": 0.01,
    "cooldown_seconds": 10,
    "floor_fps": 1,
}

# Tamaño reducido para comparar frames al detectar movimiento — barato,
# no hace falta resolución completa para saber "algo cambió acá".
MOTION_DIFF_SIZE = (160, 90)


class CameraSource:
    def __init__(self, camera_id: str, rtsp_url: str, frame_queue, inference_fps: int = INFERENCE_FPS,
                 motion_config: dict = None):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.frame_queue = frame_queue
        self.inference_fps = inference_fps
        self.min_interval_activo = 1.0 / inference_fps  # segundos mínimos entre frames encolados a ritmo normal
        self.motion_config = {**DEFAULT_MOTION_CONFIG, **(motion_config or {})}
        self.min_interval_reposo = 1.0 / max(self.motion_config["floor_fps"], 0.001)

        self._thread = None
        self._stop_event = threading.Event()
        self.status = "stopped"  # stopped | connecting | connected | reconnecting | failed

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"cam-{self.camera_id}")
        self._thread.start()
        return self

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.status = "stopped"

    def _run(self):
        reconnect_idx = 0
        while not self._stop_event.is_set():
            self.status = "connecting"
            cap = cv2.VideoCapture(self._resolver_fuente())

            if not cap.isOpened():
                self._handle_connect_failure(cap, reconnect_idx)
                reconnect_idx = min(reconnect_idx + 1, len(RECONNECT_DELAYS) - 1)
                continue

            # NOTA: se probó cap.set(cv2.CAP_PROP_FPS, ...) acá para pedirle
            # menos FPS a la cámara y ahorrar decode. Medido y descartado: en
            # esta webcam (backend MSMF de Windows) romper el formato
            # negociado hace que cap.read() deje de entregar frames por
            # completo. Si se quiere reintentar, probar con cv2.CAP_DSHOW
            # como backend explícito, no con MSMF (default en Windows).
            logger.info(f"[{self.camera_id}] Conectado: {self._safe_url()}")
            self.status = "connected"
            reconnect_idx = 0
            last_enqueued = 0.0
            last_check = 0.0
            prev_gray = None
            # Arranca en modo "activo" para no perderse los primeros segundos
            # tras conectar, hasta que el filtro de movimiento tenga referencia.
            ultimo_movimiento = time.monotonic()

            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if not ok:
                    logger.warning(f"[{self.camera_id}] Se perdió el stream, reconectando...")
                    self.status = "reconnecting"
                    break

                now = time.monotonic()

                if self.motion_config["enabled"]:
                    # El diff se recalcula al ritmo "activo" (barato, unos
                    # pocos ms), no en cada frame nativo de la cámara.
                    if (now - last_check) >= self.min_interval_activo:
                        hay_movimiento, prev_gray = self._detectar_movimiento(frame, prev_gray)
                        last_check = now
                        if hay_movimiento:
                            ultimo_movimiento = now

                    en_cooldown = (now - ultimo_movimiento) <= self.motion_config["cooldown_seconds"]
                    intervalo = self.min_interval_activo if en_cooldown else self.min_interval_reposo
                else:
                    intervalo = self.min_interval_activo

                if (now - last_enqueued) >= intervalo:
                    self._enqueue(frame, now)
                    last_enqueued = now
                # Frames descartados entre medio no se procesan (submuestreo)

            cap.release()

            if not self._stop_event.is_set():
                delay = RECONNECT_DELAYS[reconnect_idx]
                logger.info(f"[{self.camera_id}] Reintentando en {delay}s...")
                self._stop_event.wait(delay)
                reconnect_idx = min(reconnect_idx + 1, len(RECONNECT_DELAYS) - 1)

    def _detectar_movimiento(self, frame, prev_gray):
        """Compara una versión chica en escala de grises del frame contra la
        anterior. Devuelve (hay_movimiento, gray_actual). Sin frame anterior
        (recién conectada o recién reconectada) se asume movimiento, para no
        arrancar en modo reposo sin haber visto nada todavía."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, MOTION_DIFF_SIZE)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        if prev_gray is None:
            return True, gray

        diff = cv2.absdiff(prev_gray, gray)
        _, mascara = cv2.threshold(diff, self.motion_config["threshold"], 255, cv2.THRESH_BINARY)
        fraccion_cambiada = cv2.countNonZero(mascara) / mascara.size
        return fraccion_cambiada >= self.motion_config["min_area_fraction"], gray

    def _resolver_fuente(self):
        # Si rtsp_url es un número (ej. "0"), se interpreta como índice de
        # webcam local (cv2.VideoCapture(int)) en vez de URL RTSP — permite
        # probar el pipeline completo con la cámara de la PC antes de tener
        # acceso al DVR real.
        if isinstance(self.rtsp_url, int):
            return self.rtsp_url
        if isinstance(self.rtsp_url, str) and self.rtsp_url.strip().isdigit():
            return int(self.rtsp_url.strip())
        return self.rtsp_url

    def _handle_connect_failure(self, cap, reconnect_idx):
        cap.release()
        self.status = "failed"
        delay = RECONNECT_DELAYS[reconnect_idx]
        logger.warning(f"[{self.camera_id}] No se pudo conectar. Reintentando en {delay}s...")
        self._stop_event.wait(delay)

    def _enqueue(self, frame, timestamp):
        item = {
            "camera_id": self.camera_id,
            "frame": frame,
            "timestamp": timestamp,
        }
        try:
            self.frame_queue.put_nowait(item)
        except Exception:
            # Cola llena: se descarta el frame más nuevo antes que bloquear
            # (evita que una cámara lenta trabe a las demás).
            logger.debug(f"[{self.camera_id}] Cola llena, frame descartado")

    def _safe_url(self):
        # Oculta credenciales al loguear (rtsp://user:pass@ip -> rtsp://ip)
        if isinstance(self.rtsp_url, str) and "@" in self.rtsp_url:
            scheme, rest = self.rtsp_url.split("://", 1)
            _, host_part = rest.split("@", 1)
            return f"{scheme}://{host_part}"
        return self.rtsp_url
