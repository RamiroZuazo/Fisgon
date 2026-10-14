"""
benchmarks/verificar_resolucion.py
Verifica a qué resolución corre realmente el modelo cuando se lo llama como lo
hace el pipeline (inference_worker: modelo.predict(source=frames, conf, iou),
sin pasar imgsz).

Registra la forma real del tensor que entra a la sesión de ONNX Runtime, que
es la única fuente que no depende de lo que diga la configuración.

Uso:
    python benchmarks/verificar_resolucion.py models/best_2_0.onnx [otro.onnx ...]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402

from inference.model_loader import cargar_modelo  # noqa: E402

for ruta in sys.argv[1:]:
    import inference.model_loader as ml
    ml._modelo_cache = None  # forzar carga nueva por modelo
    modelo = cargar_modelo(ruta)
    sesion = modelo.predictor.model.session
    formas = []
    run_original = sesion.run

    def run_espia(salidas, entradas, *a, **k):
        for v in entradas.values():
            formas.append(tuple(v.shape))
        return run_original(salidas, entradas, *a, **k)

    sesion.run = run_espia
    frame = np.zeros((720, 1280, 3), dtype="uint8")
    modelo.predict(source=[frame], conf=0.7, iou=0.45, verbose=False)
    print(f"{ruta}")
    print(f"  metadata imgsz del modelo: {modelo.predictor.model.imgsz if hasattr(modelo.predictor.model, 'imgsz') else '?'}")
    print(f"  predictor.args.imgsz: {modelo.predictor.args.imgsz}")
    print(f"  forma real del tensor de entrada (frame 1280x720): {formas[-1] if formas else 'no capturada'}")
