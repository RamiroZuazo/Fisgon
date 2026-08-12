"""
prueba_sin_submuestreo.py
Script de prueba AISLADO (no forma parte del pipeline) para comparar contra
el comportamiento normal del edge: corre el modelo sobre TODOS los frames
que entrega la webcam, sin submuestreo de FPS ni filtro de movimiento (los
dos filtros que sí tiene capture/camera_source.py en producción).

Uso:
    python prueba_sin_submuestreo.py [segundos_de_prueba]
    (por defecto 30 segundos)

Pensado para medir, a ojo con el Administrador de tareas mientras corre, o
con los números que imprime esta misma consola, qué pasa con la CPU y con
la tasa real de fps procesados cuando se saltea la optimización de
capture/camera_source.py — ver SISTEMA.md sección 6 para el contexto
completo de por qué existe el submuestreo + filtro de movimiento en el
pipeline real.
"""

import sys
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cv2

from inference.model_loader import cargar_modelo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("prueba_sin_submuestreo")


def main():
    segundos = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

    logger.info("Cargando modelo...")
    modelo = cargar_modelo("models/best.onnx")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("No se pudo abrir la webcam (indice 0)")
        sys.exit(1)

    logger.info(f"Corriendo SIN submuestreo ni filtro de movimiento durante {segundos:.0f}s...")
    logger.info("Cada frame que se lee de la camara se manda directo al modelo, sin descartar ninguno.")

    frames_leidos = 0
    total_detecciones = 0
    t_inicio = time.monotonic()
    t_ultimo_reporte = t_inicio

    while time.monotonic() - t_inicio < segundos:
        ok, frame = cap.read()
        if not ok:
            logger.warning("No se pudo leer un frame, reintentando...")
            continue
        frames_leidos += 1

        resultados = modelo.predict(frame, conf=0.7, iou=0.45, verbose=False)[0]
        total_detecciones += len(resultados.boxes)

        ahora = time.monotonic()
        if ahora - t_ultimo_reporte >= 5.0:
            transcurrido = ahora - t_inicio
            fps_real = frames_leidos / transcurrido
            logger.info(
                f"t={transcurrido:5.1f}s  frames_procesados={frames_leidos}  "
                f"fps_real={fps_real:.2f}  detecciones_totales={total_detecciones}"
            )
            t_ultimo_reporte = ahora

    cap.release()

    transcurrido = time.monotonic() - t_inicio
    logger.info("=== Resumen ===")
    logger.info(f"Duracion: {transcurrido:.1f}s")
    logger.info(f"Frames procesados por el modelo (TODOS, sin descartar ninguno): {frames_leidos}")
    logger.info(f"FPS real sostenido: {frames_leidos/transcurrido:.2f}")
    logger.info(f"Detecciones totales (sin validador temporal de por medio): {total_detecciones}")
    logger.info("Comparar este fps_real contra los 5fps configurados en cameras.yaml (inference_fps) "
                "y contra el 17-18fps de techo medido en CONTEXT.md.")


if __name__ == "__main__":
    main()
