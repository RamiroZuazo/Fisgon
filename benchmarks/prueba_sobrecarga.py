"""
benchmarks/prueba_sobrecarga.py
Prueba de estrés de la cola de frames: qué pasa con la frescura de lo que
analiza el modelo cuando otra carga le quita CPU al pipeline.

Arma las piezas reales del pipeline —CameraManager con las cámaras de
cameras.yaml, la cola compartida e InferenceWorker con el modelo— y corre tres
fases:

    normal       60 s sin interferencia
    sobrecarga   60 s con un proceso por núcleo girando al 100 %
    recuperación 60 s sin interferencia

Por cada frame que llega al modelo registra su antigüedad: cuánto pasó desde
que se capturó hasta que se procesó. Es la parte de la latencia de alerta que
depende de la cola (RNF001: alerta en menos de 3 s). Cada segundo registra
además el tamaño de la cola y la memoria del proceso.

Sirve para comparar dos políticas de cola: la original (200 frames,
descartando el frame nuevo al llenarse) y la corregida (un segundo de frames,
descartando el más viejo). La política la decide el código de
capture/camera_source.py y main.py; este script solo usa el tamaño de cola
que se le pase.

IMPORTANTE: correr con el pipeline detenido (usa las mismas cámaras y la GPU).

Uso:
    python benchmarks/prueba_sobrecarga.py --cola 200 --salida benchmarks/resultados/b4_sobrecarga_antes
"""

import argparse
import json
import multiprocessing
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Queue

RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import psutil  # noqa: E402
import yaml  # noqa: E402

from inference.model_loader import cargar_modelo  # noqa: E402
from capture.camera_manager import CameraManager  # noqa: E402
from inference.inference_worker import InferenceWorker  # noqa: E402
from medir_proceso import resumen  # noqa: E402

FASES = (("normal", False), ("sobrecarga", True), ("recuperacion", False))


def girar(fin):
    """Carga sintética: un núcleo al 100 % hasta que se pida parar."""
    x = 0
    while not fin.is_set():
        for _ in range(100000):
            x += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cola", type=int, required=True, help="tamaño máximo de la cola de frames")
    parser.add_argument("--salida", required=True)
    parser.add_argument("--segundos", type=float, default=60, help="duración de cada fase")
    parser.add_argument("--config", default=str(RAIZ / "cameras.yaml"))
    args = parser.parse_args()

    config = yaml.safe_load(open(args.config, encoding="utf-8"))
    proceso = psutil.Process()
    cola = Queue(maxsize=args.cola)

    modelo = cargar_modelo(str(RAIZ / config["inference"]["model_path"]))
    worker = InferenceWorker(frame_queue=cola, modelo=modelo, config=config, on_detection=lambda d: None)

    # Se envuelve el procesamiento de lotes para anotar la antigüedad de cada
    # frame justo antes de que entre al modelo.
    edades = []  # (t relativo, fase, edad_s)
    fase_actual = {"nombre": "arranque"}
    t0 = time.monotonic()
    original = worker._procesar_lote

    def procesar_y_medir(lote):
        ahora = time.monotonic()
        for item in lote:
            edades.append((ahora - t0, fase_actual["nombre"], ahora - item["timestamp"]))
        original(lote)

    worker._procesar_lote = procesar_y_medir

    manager = CameraManager(config, frame_queue=cola)
    worker.start()
    manager.start_all()

    print("Esperando a que conecten las cámaras (15 s)...")
    time.sleep(15)
    edades.clear()
    t0 = time.monotonic()

    muestras = []
    fin_muestreo = threading.Event()

    def muestrear():
        while not fin_muestreo.wait(1.0):
            muestras.append({
                "t": round(time.monotonic() - t0, 1),
                "fase": fase_actual["nombre"],
                "cola": cola.qsize(),
                "rss_mb": round(proceso.memory_info().rss / 2**20, 1),
            })

    hilo = threading.Thread(target=muestrear, daemon=True)
    hilo.start()

    for nombre, con_carga in FASES:
        fase_actual["nombre"] = nombre
        print(f"Fase {nombre} ({args.segundos:.0f} s)...")
        if con_carga:
            fin_carga = multiprocessing.Event()
            giradores = [multiprocessing.Process(target=girar, args=(fin_carga,), daemon=True)
                         for _ in range(psutil.cpu_count(logical=True))]
            for g in giradores:
                g.start()
            time.sleep(args.segundos)
            fin_carga.set()
            for g in giradores:
                g.join(timeout=5)
        else:
            time.sleep(args.segundos)

    fin_muestreo.set()
    hilo.join()
    manager.stop_all()
    worker.stop()

    salida = Path(args.salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    with open(salida.with_name(salida.name + "_cola.csv"), "w", encoding="utf-8") as f:
        f.write("t,fase,cola,rss_mb\n")
        for m in muestras:
            f.write(f"{m['t']},{m['fase']},{m['cola']},{m['rss_mb']}\n")
    with open(salida.with_name(salida.name + "_edades.csv"), "w", encoding="utf-8") as f:
        f.write("t,fase,edad_s\n")
        for t, fase, edad in edades:
            f.write(f"{t:.2f},{fase},{edad:.3f}\n")

    por_fase = {}
    for nombre, _ in FASES:
        e = [edad for _, fase, edad in edades if fase == nombre]
        m = [x for x in muestras if x["fase"] == nombre]
        por_fase[nombre] = {
            "frames_procesados": len(e),
            "frames_por_s": round(len(e) / args.segundos, 2),
            "edad_frame_s": resumen(e),
            "cola_max": max((x["cola"] for x in m), default=None),
            "rss_mb_max": max((x["rss_mb"] for x in m), default=None),
        }

    informe = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "tamanio_cola": args.cola,
        "camaras": [c["id"] for c in config["cameras"] if c.get("active")],
        "segundos_por_fase": args.segundos,
        "fases": por_fase,
    }
    salida.with_suffix(".json").write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(informe, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
