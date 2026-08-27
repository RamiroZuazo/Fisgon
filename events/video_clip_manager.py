"""
events/video_clip_manager.py
Mantiene un buffer circular de video por cámara y genera clips WebM/VP8 sin
bloquear la captura ni la inferencia.

Cada CameraSource entrega frames a ``add_frame``. Cuando EventValidator
confirma una detección, ``request_clip`` espera los segundos posteriores
configurados y arma un video con la ventana completa alrededor del evento.
El callback recibe el WebM codificado en base64, o ``None`` si no pudo
generarse; así el evento nunca queda trabado indefinidamente.
"""

import base64
import logging
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger("video_clip_manager")

DEFAULT_VIDEO_CONFIG = {
    "enabled": True,
    "seconds_before": 10,
    "seconds_after": 10,
    "fps": 5,
    "max_width": 960,
    "jpeg_quality": 80,
}


class VideoClipManager:
    """Buffer circular y codificador asíncrono de clips por cámara."""

    def __init__(self, config: dict):
        self.config = {**DEFAULT_VIDEO_CONFIG, **config.get("event_video", {})}
        self.enabled = bool(self.config["enabled"])
        self.seconds_before = max(float(self.config["seconds_before"]), 0.0)
        self.seconds_after = max(float(self.config["seconds_after"]), 0.0)
        self.fps = max(float(self.config["fps"]), 1.0)
        self.max_width = max(int(self.config["max_width"]), 160)
        self.jpeg_quality = min(max(int(self.config["jpeg_quality"]), 30), 95)

        self._buffers = defaultdict(deque)
        self._last_frame_at = defaultdict(float)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._jobs = set()

    def add_frame(self, camera_id: str, frame, timestamp: float):
        """Guarda una copia JPEG a ``fps`` dentro del buffer circular."""
        if not self.enabled or self._stop_event.is_set():
            return

        with self._lock:
            if timestamp - self._last_frame_at[camera_id] < 1.0 / self.fps:
                return
            self._last_frame_at[camera_id] = timestamp

        frame_reducido = self._redimensionar(frame)
        ok, jpeg = cv2.imencode(
            ".jpg", frame_reducido, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        )
        if not ok:
            logger.warning("[%s] No se pudo almacenar un frame para el clip", camera_id)
            return

        retencion = self.seconds_before + self.seconds_after + 5
        with self._lock:
            buffer = self._buffers[camera_id]
            buffer.append((timestamp, jpeg.tobytes()))
            limite = timestamp - retencion
            while buffer and buffer[0][0] < limite:
                buffer.popleft()

    def request_clip(self, camera_id: str, detection_timestamp: float, on_ready):
        """Solicita el clip y llama ``on_ready(video_base64 | None)`` al finalizar."""
        if not self.enabled:
            on_ready(None)
            return

        job = threading.Thread(
            target=self._generar_despues,
            args=(camera_id, detection_timestamp, on_ready),
            daemon=True,
            name=f"clip-{camera_id}-{int(detection_timestamp)}",
        )
        with self._lock:
            self._jobs.add(job)
        job.start()

    def stop(self):
        self._stop_event.set()
        with self._lock:
            jobs = list(self._jobs)
        for job in jobs:
            job.join(timeout=2)

    def _generar_despues(self, camera_id: str, detection_timestamp: float, on_ready):
        video_base64 = None
        try:
            if self._stop_event.wait(self.seconds_after):
                return
            inicio = detection_timestamp - self.seconds_before
            fin = detection_timestamp + self.seconds_after
            with self._lock:
                frames = [data for ts, data in self._buffers[camera_id] if inicio <= ts <= fin]

            minimo_esperado = max(2, int((self.seconds_before + self.seconds_after) * self.fps * 0.25))
            if len(frames) < minimo_esperado:
                logger.warning(
                    "[%s] No hay suficientes frames para el clip (%s, mínimo %s)",
                    camera_id,
                    len(frames),
                    minimo_esperado,
                )
            elif video_bytes := self._codificar_webm(frames):
                video_base64 = base64.b64encode(video_bytes).decode("ascii")
                logger.info(
                    "[%s] Clip generado: %.0fs antes + %.0fs después (%s frames, %.1f MB)",
                    camera_id,
                    self.seconds_before,
                    self.seconds_after,
                    len(frames),
                    len(video_bytes) / (1024 * 1024),
                )
        except Exception:
            logger.exception("[%s] No se pudo generar el clip del evento", camera_id)
        finally:
            try:
                on_ready(video_base64)
            finally:
                with self._lock:
                    self._jobs.discard(threading.current_thread())

    def _redimensionar(self, frame):
        alto, ancho = frame.shape[:2]
        if ancho <= self.max_width:
            return frame
        escala = self.max_width / ancho
        return cv2.resize(frame, (self.max_width, int(alto * escala)), interpolation=cv2.INTER_AREA)

    def _codificar_webm(self, frames_jpeg: list[bytes]) -> bytes | None:
        primero = cv2.imdecode(np.frombuffer(frames_jpeg[0], dtype="uint8"), cv2.IMREAD_COLOR)
        if primero is None:
            return None
        alto, ancho = primero.shape[:2]

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as temp:
                temp_path = Path(temp.name)
            writer = cv2.VideoWriter(
                str(temp_path), cv2.VideoWriter_fourcc(*"VP80"), self.fps, (ancho, alto)
            )
            if not writer.isOpened():
                logger.error("OpenCV no pudo abrir el codec WebM/VP8")
                return None
            try:
                for jpeg in frames_jpeg:
                    frame = cv2.imdecode(np.frombuffer(jpeg, dtype="uint8"), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    if frame.shape[1] != ancho or frame.shape[0] != alto:
                        frame = cv2.resize(frame, (ancho, alto))
                    writer.write(frame)
            finally:
                writer.release()
            return temp_path.read_bytes() if temp_path.stat().st_size > 0 else None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
