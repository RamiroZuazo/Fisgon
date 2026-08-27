"""
events/event_validator.py
Confirma "eventos reales" a partir de detecciones individuales, para evitar
falsos positivos de un solo frame.

Mantiene un historial corto de timestamps por (camera_id, object_class). Un
evento se confirma cuando hay al menos `min_consecutive_detections` dentro
de los últimos `window_seconds` segundos. Al confirmar, arma el payload
final (con snapshot en base64 y bbox en formato {x, y, width, height}, que
es el que espera la API — ver upload/upload_worker.py) y lo pasa a
on_event_confirmed (normalmente local_buffer.save_pending).

Nota de diseño: el timestamp que llega en cada detección (item de la cola de
camera_source.py) es time.monotonic(), útil solo para medir duraciones
dentro del proceso. La API asigna su propio timestamp al recibir el evento,
así que acá se guarda uno real (UTC, ISO 8601) solo para uso/registro local
(local_buffer), no se manda a la API.

Uso típico (ver main.py):
    from events.event_validator import EventValidator
    validador = EventValidator(config, on_event_confirmed=buffer.save_pending)
    inference_worker = InferenceWorker(..., on_detection=validador.procesar_deteccion)
"""

import base64
import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone

import cv2

logger = logging.getLogger("event_validator")


class EventValidator:
    def __init__(self, config: dict, on_event_confirmed):
        ev_cfg = config.get("event_validation", {})
        self.min_consecutive = ev_cfg.get("min_consecutive_detections", 3)
        self.window_seconds = ev_cfg.get("window_seconds", 5)
        self.on_event_confirmed = on_event_confirmed

        # historial[(camera_id, object_class)] = deque de timestamps monotonic
        self._historial = defaultdict(deque)
        # evita re-confirmar el mismo evento en cada frame mientras dura la racha
        self._confirmados_recientes = {}
        self._lock = threading.Lock()

    def procesar_deteccion(self, deteccion: dict):
        camera_id = deteccion["camera_id"]
        object_class = deteccion["object_class"]
        timestamp = deteccion["timestamp"]
        clave = (camera_id, object_class)

        with self._lock:
            historial = self._historial[clave]
            historial.append(timestamp)

            # descarta detecciones fuera de la ventana
            limite = timestamp - self.window_seconds
            while historial and historial[0] < limite:
                historial.popleft()

            if len(historial) < self.min_consecutive:
                return

            # ya se confirmó un evento reciente para esta cámara/clase: no duplicar
            ultimo_confirmado = self._confirmados_recientes.get(clave, 0)
            if timestamp - ultimo_confirmado < self.window_seconds:
                return

            self._confirmados_recientes[clave] = timestamp
            frames_validated = len(historial)

        logger.info(
            f"[{camera_id}] Evento confirmado: {object_class} "
            f"({frames_validated} detecciones en {self.window_seconds}s)"
        )
        self._confirmar_evento(deteccion, frames_validated)

    @staticmethod
    def _bbox_a_xywh(bbox_xyxy) -> dict:
        """Convierte [x1, y1, x2, y2] (formato de inference_worker/YOLO) al
        formato {x, y, width, height} que espera la API."""
        x1, y1, x2, y2 = bbox_xyxy
        return {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}

    @staticmethod
    def _dibujar_deteccion(frame, bbox_xyxy, object_class: str, confidence: float):
        """Dibuja el bounding box + etiqueta sobre una copia del frame, para
        que el snapshot que se sube muestre qué detectó el modelo (igual
        idea que resultados[0].plot() en fisgon_amd.py)."""
        x1, y1, x2, y2 = [int(round(v)) for v in bbox_xyxy]
        anotado = frame.copy()
        cv2.rectangle(anotado, (x1, y1), (x2, y2), (0, 0, 255), 2)
        etiqueta = f"{object_class} {confidence:.2f}"
        (ancho_txt, alto_txt), _ = cv2.getTextSize(etiqueta, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        y_etiqueta = max(y1 - 8, alto_txt + 4)
        cv2.rectangle(anotado, (x1, y_etiqueta - alto_txt - 6), (x1 + ancho_txt + 6, y_etiqueta + 2), (0, 0, 255), -1)
        cv2.putText(anotado, etiqueta, (x1 + 3, y_etiqueta - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        return anotado

    def _confirmar_evento(self, deteccion: dict, frames_validated: int):
        frame_anotado = self._dibujar_deteccion(
            deteccion["frame"], deteccion["bbox"], deteccion["object_class"], deteccion["confidence"]
        )
        ok, buffer_jpg = cv2.imencode(".jpg", frame_anotado)
        snapshot_base64 = base64.b64encode(buffer_jpg).decode("ascii") if ok else None

        payload = {
            "camera_id": deteccion["camera_id"],
            "object_class": deteccion["object_class"],
            "confidence": deteccion["confidence"],
            "bbox": self._bbox_a_xywh(deteccion["bbox"]),
            "frames_validated": frames_validated,
            "snapshot_base64": snapshot_base64,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            # Solo se usa localmente para centrar el clip. upload_worker no lo envía.
            "detection_monotonic": deteccion["timestamp"],
        }
        self.on_event_confirmed(payload)


if __name__ == "__main__":
    # Prueba aislada: simula detecciones consecutivas y verifica que confirme el evento
    import time

    import numpy as np

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    eventos = []
    config = {"event_validation": {"min_consecutive_detections": 3, "window_seconds": 5}}
    validador = EventValidator(config, on_event_confirmed=eventos.append)

    frame_dummy = np.zeros((10, 10, 3), dtype="uint8")
    base_ts = time.monotonic()
    for i in range(3):
        validador.procesar_deteccion({
            "camera_id": "cam_test",
            "object_class": "pistol",
            "confidence": 0.9,
            "bbox": [0, 0, 5, 5],
            "frame": frame_dummy,
            "timestamp": base_ts + i * 0.5,
        })

    print(f"Eventos confirmados: {len(eventos)}")
    for e in eventos:
        print(f"  {e}")
