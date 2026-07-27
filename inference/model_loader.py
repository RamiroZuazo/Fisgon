"""
inference/model_loader.py
Carga del modelo YOLOv8 (ONNX) con aceleración DirectML (GPU AMD).

Reusa activar_directml() y resolver_modelo() de fisgon_amd.py (mismo
patcheo de onnxruntime.InferenceSession que usa el script de referencia).
El modelo se carga UNA sola vez y se comparte entre todos los frames que
procese inference_worker.py — nunca se re-crea la sesión ONNX por cámara.

Uso:
    from inference.model_loader import cargar_modelo
    modelo = cargar_modelo("models/best.onnx")
    resultados = modelo.predict(frame, conf=0.5, iou=0.45, verbose=False)
"""

import logging
import os
import sys
import threading
from pathlib import Path

# onnxruntime-directml y onnxruntime (CPU) se registran bajo el mismo nombre
# de import ("onnxruntime") pero son paquetes pip distintos. Ultralytics
# revisa metadata de pip por nombre de distribución, no encuentra
# "onnxruntime" (porque lo que hay instalado es "onnxruntime-directml") y
# intenta auto-instalar el paquete CPU con pip — lo que pisa la instalación
# DirectML y tira la GPU AMD abajo. Esta variable desactiva ese chequeo.
# Tiene que setearse ANTES de importar ultralytics.
os.environ.setdefault("ULTRALYTICS_SKIP_REQUIREMENTS_CHECKS", "1")

# Permite ejecutar este archivo directamente además de importarlo como
# inference.model_loader desde main.py
sys.path.insert(0, str(Path(__file__).parent.parent))

from fisgon_amd import activar_directml, resolver_modelo

logger = logging.getLogger("model_loader")

_lock = threading.Lock()
_modelo_cache = None
_ruta_cache = None


def cargar_modelo(model_path: str):
    """Carga el modelo (con cache) y activa DirectML antes de crear la sesión ONNX."""
    global _modelo_cache, _ruta_cache

    ruta = Path(model_path)
    with _lock:
        if _modelo_cache is not None and _ruta_cache == ruta:
            return _modelo_cache

        try:
            from ultralytics import YOLO
        except ImportError:
            logger.error("ultralytics no está instalado. Instalalo con: pip install ultralytics")
            raise

        if not ruta.exists():
            raise FileNotFoundError(f"No se encontró el modelo: {ruta.resolve()}")

        activar_directml()
        ruta_onnx = resolver_modelo(ruta)

        logger.info(f"Cargando modelo: {ruta_onnx}")
        modelo = YOLO(str(ruta_onnx))
        logger.info(f"Modelo cargado. Clases: {modelo.names}")

        _loguear_proveedor_real(modelo)

        _modelo_cache = modelo
        _ruta_cache = ruta
        return modelo


def _loguear_proveedor_real(modelo):
    """Ultralytics imprime un mensaje de log ('Using ONNX Runtime ... with
    CPUExecutionProvider') que refleja lo que Ultralytics INTENTA usar antes
    de que activar_directml() lo pise — no el proveedor real de la sesión.
    Acá se fuerza la creación de la sesión (con un frame negro de prueba) y
    se consulta session.get_providers() directamente, así queda un registro
    confiable en logs/edge.log de si la GPU AMD está siendo usada o no."""
    try:
        import numpy as np

        modelo.predict(np.zeros((640, 640, 3), dtype="uint8"), verbose=False)
        session = modelo.predictor.model.session
        proveedores = session.get_providers()
        if "DmlExecutionProvider" in proveedores:
            logger.info(f"Inferencia corriendo por GPU (DirectML). Proveedores: {proveedores}")
        else:
            logger.warning(f"Inferencia corriendo por CPU, no se activó DirectML. Proveedores: {proveedores}")
    except Exception:
        logger.exception("No se pudo verificar el proveedor ONNX real (no es crítico, se sigue igual)")


if __name__ == "__main__":
    # Prueba aislada: carga el modelo y muestra sus clases.
    # Uso: python inference/model_loader.py models/best.onnx
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    if len(sys.argv) < 2:
        print("Uso: python model_loader.py <ruta_modelo.onnx|.pt>")
        sys.exit(1)

    modelo = cargar_modelo(sys.argv[1])
    print(f"Modelo cargado correctamente. Clases: {modelo.names}")
