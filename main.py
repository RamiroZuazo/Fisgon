"""
main.py
Punto de entrada del pipeline edge: cámaras → inferencia → validación de
eventos → buffer local → subida a la nube.

Uso:
    python main.py --config cameras.yaml

Arranca camera_manager (una CameraSource por cámara activa), un único
inference_worker consumiendo la cola compartida, event_validator confirmando
eventos reales, local_buffer persistiéndolos en SQLite, y upload_worker
subiéndolos a la API en la nube. Ctrl+C cierra todo ordenadamente.
"""

import argparse
import logging
import signal
import sys
import threading
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from queue import Queue

import yaml
from dotenv import load_dotenv

from capture.camera_manager import CameraManager
from events.event_validator import EventValidator
from events.local_buffer import LocalBuffer
from inference.inference_worker import InferenceWorker
from inference.model_loader import cargar_modelo
from upload.upload_worker import UploadWorker

logger = logging.getLogger("main")


def parsear_args():
    parser = argparse.ArgumentParser(description="Edge: captura + inferencia + eventos + subida")
    parser.add_argument("--config", type=str, default="cameras.yaml", help="Ruta a cameras.yaml")
    return parser.parse_args()


LOG_RETENTION_DIAS = 14  # para 24/7: sin esto logs/edge.log crece sin límite


def configurar_logging():
    Path("logs").mkdir(exist_ok=True)
    # Rota a medianoche, se queda con los últimos LOG_RETENTION_DIAS archivos
    # y borra el resto solo — necesario para correr semanas sin intervención.
    file_handler = TimedRotatingFileHandler(
        "logs/edge.log", when="midnight", backupCount=LOG_RETENTION_DIAS, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            file_handler,
        ],
    )


def cargar_config(config_path: str) -> dict:
    ruta = Path(config_path)
    if not ruta.exists():
        logger.error(f"No se encontró el archivo de configuración: {ruta.resolve()}")
        sys.exit(1)
    with open(ruta, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    load_dotenv()  # lee .env (FISGON_API_URL, FISGON_EDGE_TOKEN) si existe junto al proyecto
    configurar_logging()
    args = parsear_args()
    config = cargar_config(args.config)

    logger.info("=== Iniciando pipeline edge ===")

    frame_queue = Queue(maxsize=200)

    logger.info("Cargando modelo YOLOv8 (ONNX + DirectML)...")
    modelo = cargar_modelo(config["inference"]["model_path"])

    buffer = LocalBuffer("eventos.db")
    upload_worker = UploadWorker(buffer, config)

    validador = EventValidator(config, on_event_confirmed=buffer.save_pending)

    inference_worker = InferenceWorker(
        frame_queue=frame_queue,
        modelo=modelo,
        config=config,
        on_detection=validador.procesar_deteccion,
    )

    camera_manager = CameraManager(config, frame_queue=frame_queue)

    stop_event_principal = threading.Event()

    def manejar_señal(signum, frame):
        logger.info("Señal de interrupción recibida, cerrando pipeline...")
        stop_event_principal.set()

    signal.signal(signal.SIGINT, manejar_señal)
    try:
        signal.signal(signal.SIGTERM, manejar_señal)
    except (AttributeError, ValueError):
        pass  # SIGTERM no disponible en todas las plataformas (ej. Windows fuera del hilo principal)

    upload_worker.start()
    inference_worker.start()
    camera_manager.start_all()

    logger.info("Pipeline en marcha. Ctrl+C para detener.")
    try:
        while not stop_event_principal.is_set():
            stop_event_principal.wait(5)
            if not stop_event_principal.is_set():
                logger.info(f"Estado cámaras: {camera_manager.get_status()} | frames en cola: {frame_queue.qsize()}")
    finally:
        logger.info("Deteniendo cámaras...")
        camera_manager.stop_all()
        logger.info("Deteniendo worker de inferencia...")
        inference_worker.stop()
        logger.info("Deteniendo worker de subida...")
        upload_worker.stop()
        buffer.close()
        logger.info("=== Pipeline detenido ===")


if __name__ == "__main__":
    main()
