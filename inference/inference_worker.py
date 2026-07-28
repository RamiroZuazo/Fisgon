"""
inference/inference_worker.py
Worker único que consume la cola compartida de frames y corre inferencia
YOLOv8 sobre lotes de frames (de una o varias cámaras a la vez).

Corre en un solo thread a propósito: nunca hay que llamar modelo.predict()
desde dos threads a la vez sobre la misma sesión ONNX (ver CONTEXT.md).
Todas las cámaras comparten este worker.

Batching: cada vuelta del loop toma el primer frame disponible (esperando
si hace falta) y después junta, SIN esperar de más, todo lo que ya esté
esperando en la cola en ese momento (hasta max_batch_size). Si solo hay 1
cámara o el worker va al día, el lote suele ser de 1 frame — mismo
comportamiento de antes, sin penalidad. Si hay varias cámaras y se acumuló
algo de cola, se procesan juntas en una sola llamada al modelo.

Por qué importa: medido en este proyecto, cada llamada a modelo.predict()
tiene un costo fijo de CPU (~45ms) que no depende mucho de cuántos frames
se le pasen de una vez — juntar 4 frames en una llamada cuesta bastante
menos que 4 llamadas separadas (ver CONTEXT.md / discusión de rendimiento).

Por cada resultado:
  - Por cada detección con confidence >= umbral, arma un dict de detección
    y lo pasa a on_detection() (normalmente event_validator.procesar_deteccion)

Uso típico (ver main.py):
    from inference.inference_worker import InferenceWorker
    worker = InferenceWorker(frame_queue, modelo, config, on_detection=validador.procesar_deteccion)
    worker.start()
    ...
    worker.stop()
"""

import logging
import queue
import threading
from collections import defaultdict

logger = logging.getLogger("inference_worker")

# Supuesto de diseño: si el modelo ONNX no trae `names` embebidos, se asume
# el mapeo de clases del dataset de la tesis (pistola/cuchillo). Ajustar acá
# si el modelo final usa otro orden de clases.
CLASS_NAMES_FALLBACK = {0: "pistol", 1: "knife"}

# El modelo entrenado (best.onnx) usa "gun" como nombre de clase, pero la API
# en la nube espera el enum "pistol" (ver CONTEXT.md, tabla `events`). Se
# traduce acá antes de pasar la detección a event_validator, sin tocar el
# modelo ni la API.
CLASS_NAME_API_MAP = {"gun": "pistol"}

# Tope de frames por llamada al modelo. 4 porque es el máximo de cámaras
# soportado (ver CONTEXT.md) — no tiene sentido batchear más que eso.
DEFAULT_MAX_BATCH_SIZE = 4


class InferenceWorker:
    def __init__(self, frame_queue, modelo, config: dict, on_detection):
        self.frame_queue = frame_queue
        self.modelo = modelo
        self.confidence = config.get("inference", {}).get("confidence", 0.5)
        self.iou = config.get("inference", {}).get("iou", 0.45)
        self.max_batch_size = config.get("inference", {}).get("max_batch_size", DEFAULT_MAX_BATCH_SIZE)
        self.on_detection = on_detection

        self._thread = None
        self._stop_event = threading.Event()
        # frames que efectivamente pasaron por modelo.predict(), por cámara —
        # para poder confirmar (ej. en logs) que una cámara dada llega a
        # correr por el modelo y no solo se conecta/encola.
        self._frames_procesados = defaultdict(int)
        self._stats_lock = threading.Lock()

    def get_stats(self) -> dict:
        """{camera_id: frames procesados por el modelo desde que arrancó el worker}."""
        with self._stats_lock:
            return dict(self._frames_procesados)

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="inference-worker")
        self._thread.start()
        return self

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self):
        logger.info("Worker de inferencia iniciado")
        while not self._stop_event.is_set():
            try:
                primero = self.frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            lote = [primero]
            while len(lote) < self.max_batch_size:
                try:
                    lote.append(self.frame_queue.get_nowait())
                except queue.Empty:
                    break

            try:
                self._procesar_lote(lote)
            except Exception:
                logger.exception(f"Error corriendo inferencia sobre un lote de {len(lote)} frame(s)")

        logger.info("Worker de inferencia detenido")

    def _procesar_lote(self, lote: list):
        frames = [item["frame"] for item in lote]

        resultados = self.modelo.predict(
            source=frames,
            conf=self.confidence,
            iou=self.iou,
            verbose=False,
        )

        with self._stats_lock:
            for item in lote:
                self._frames_procesados[item["camera_id"]] += 1
        logger.debug(f"Lote procesado: {[item['camera_id'] for item in lote]}")

        for item, resultado in zip(lote, resultados):
            self._procesar_resultado(item, resultado)

    def _procesar_resultado(self, item: dict, resultado):
        camera_id = item["camera_id"]
        frame = item["frame"]
        timestamp = item["timestamp"]

        nombres = getattr(resultado, "names", None) or CLASS_NAMES_FALLBACK

        for box in resultado.boxes:
            confidence = float(box.conf[0])
            if confidence < self.confidence:
                continue

            clase_id = int(box.cls[0])
            object_class = nombres.get(clase_id, str(clase_id))
            object_class = CLASS_NAME_API_MAP.get(object_class, object_class)
            bbox = [round(x, 1) for x in box.xyxy[0].tolist()]

            deteccion = {
                "camera_id": camera_id,
                "object_class": object_class,
                "confidence": confidence,
                "bbox": bbox,
                "frame": frame,
                "timestamp": timestamp,
            }
            self.on_detection(deteccion)


if __name__ == "__main__":
    # Prueba aislada con una imagen de prueba (no necesita cámaras ni cola real)
    # Uso: python inference/inference_worker.py <modelo.onnx> <imagen.jpg>
    import sys
    import time
    from pathlib import Path
    from queue import Queue

    sys.path.insert(0, str(Path(__file__).parent.parent))

    import cv2

    from inference.model_loader import cargar_modelo

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    if len(sys.argv) < 3:
        print("Uso: python inference_worker.py <modelo.onnx> <imagen.jpg>")
        sys.exit(1)

    modelo_test = cargar_modelo(sys.argv[1])
    frame_test = cv2.imread(sys.argv[2])
    if frame_test is None:
        print(f"No se pudo leer la imagen: {sys.argv[2]}")
        sys.exit(1)

    detecciones = []
    worker = InferenceWorker(
        frame_queue=Queue(),
        modelo=modelo_test,
        config={"inference": {"confidence": 0.5, "iou": 0.45}},
        on_detection=detecciones.append,
    )
    worker._procesar_lote([{"camera_id": "test", "frame": frame_test, "timestamp": time.time()}])

    print(f"Detecciones: {len(detecciones)}")
    for d in detecciones:
        print(f"  {d['object_class']} conf={d['confidence']:.2f} bbox={d['bbox']}")
