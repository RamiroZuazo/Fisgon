"""
benchmarks/bench_inferencia.py
Benchmark controlado del modelo en el equipo del edge, sin cámaras ni cola:
aísla el costo de modelo.predict(), que es donde vive casi todo el consumo
del pipeline (SISTEMA.md, sección 6).

Mide tres cosas:

1. Latencia por llamada según el tamaño de lote (1 a 4 frames): demuestra
   por qué agrupar los frames de varias cámaras en una sola llamada reduce el
   costo por frame.
2. Techo de rendimiento: cuántos frames por segundo procesa el modelo
   llamándolo sin pausa, con lote 1 y con lote 4.
3. CPU sostenida a ritmo fijo, que simula el peor caso del pipeline: todas
   las cámaras con movimiento permanente, sin que el filtro de movimiento
   descarte nada. Se compara 1 cámara, 4 cámaras sin agrupar (20 llamadas/s
   de 1 frame) y 4 cámaras agrupadas (5 llamadas/s de 4 frames).

Los frames son capturas reales de las cámaras configuradas en cameras.yaml;
si no hay ninguna disponible se usan imágenes del conjunto de prueba. El
contenido casi no cambia el costo (el NMS es <5 % del tiempo, sección 6),
pero se usan imágenes reales para que el preprocesado sea el de producción.

IMPORTANTE: correr con el pipeline detenido. Si main.py está usando la GPU
al mismo tiempo, las dos cargas se mezclan y los números no valen.

Uso:
    python benchmarks/bench_inferencia.py --salida benchmarks/resultados/b2_inferencia
"""

import argparse
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import cv2  # noqa: E402
import psutil  # noqa: E402
import yaml  # noqa: E402

from inference.model_loader import cargar_modelo  # noqa: E402  (fija KMP_BLOCKTIME y DirectML como en producción)
from medir_proceso import nombre_procesador, resumen  # noqa: E402

CONF = 0.7
IOU = 0.45


def capturar_frames(config: dict, cantidad: int = 4) -> tuple[list, str]:
    """Un frame por cámara activa, para que cada lote mezcle escenas reales."""
    frames = []
    for cam in config.get("cameras", []):
        if not cam.get("active") or len(frames) >= cantidad:
            continue
        url = cam["rtsp_url"]
        cap = cv2.VideoCapture(int(url) if str(url).isdigit() else url)
        ok, frame = cap.read() if cap.isOpened() else (False, None)
        cap.release()
        if ok:
            frames.append(frame)
            print(f"  frame de {cam['id']}: {frame.shape[1]}x{frame.shape[0]}")
    if frames:
        while len(frames) < cantidad:
            frames.append(frames[len(frames) % len(frames)])
        return frames, "capturas de las cámaras configuradas"
    sys.exit("No se pudo capturar ningún frame de las cámaras configuradas")


def latencia_por_lote(modelo, frames, repeticiones: int) -> dict:
    resultado = {}
    for tamanio in (1, 2, 3, 4):
        lote = frames[:tamanio]
        for _ in range(5):  # calentamiento: la primera llamada de cada forma compila el grafo
            modelo.predict(source=lote, conf=CONF, iou=IOU, verbose=False)
        tiempos = []
        for _ in range(repeticiones):
            t = time.perf_counter()
            modelo.predict(source=lote, conf=CONF, iou=IOU, verbose=False)
            tiempos.append((time.perf_counter() - t) * 1000)
        resultado[tamanio] = {
            "ms_por_llamada": resumen(tiempos),
            "ms_por_frame": round(statistics.mean(tiempos) / tamanio, 2),
        }
        print(f"  lote {tamanio}: {resultado[tamanio]['ms_por_llamada']['media']} ms/llamada, "
              f"{resultado[tamanio]['ms_por_frame']} ms/frame")
    return resultado


def techo_rendimiento(modelo, frames, tamanio: int, segundos: float) -> float:
    lote = frames[:tamanio]
    procesados = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < segundos:
        modelo.predict(source=lote, conf=CONF, iou=IOU, verbose=False)
        procesados += tamanio
    fps = round(procesados / (time.perf_counter() - t0), 2)
    print(f"  lote {tamanio}: {fps} frames/s")
    return fps


def cpu_a_ritmo_fijo(modelo, frames, llamadas_por_s: float, tamanio: int, segundos: float) -> dict:
    """Llama al modelo a ritmo constante y mide la CPU del propio proceso."""
    proceso = psutil.Process()
    nucleos = psutil.cpu_count(logical=True)
    lote = frames[:tamanio]
    periodo = 1.0 / llamadas_por_s
    muestras, atrasadas = [], 0
    fin = threading.Event()

    def muestrear():
        proceso.cpu_percent(None)
        while not fin.wait(2.0):
            muestras.append(proceso.cpu_percent(None) / nucleos)

    hilo = threading.Thread(target=muestrear, daemon=True)
    hilo.start()
    proxima = time.perf_counter()
    t0 = proxima
    llamadas = 0
    while time.perf_counter() - t0 < segundos:
        modelo.predict(source=lote, conf=CONF, iou=IOU, verbose=False)
        llamadas += 1
        proxima += periodo
        espera = proxima - time.perf_counter()
        if espera > 0:
            time.sleep(espera)
        else:
            atrasadas += 1
            proxima = time.perf_counter()  # no acumular atraso
    duracion = time.perf_counter() - t0
    fin.set()
    hilo.join()

    datos = {
        "llamadas_por_s_objetivo": llamadas_por_s,
        "frames_por_llamada": tamanio,
        "frames_por_s_logrados": round(llamadas * tamanio / duracion, 2),
        "llamadas_atrasadas_pct": round(100 * atrasadas / max(llamadas, 1), 1),
        "cpu_proceso_pct": resumen(muestras),
    }
    print(f"  {llamadas_por_s} llamadas/s x {tamanio} frame(s): "
          f"CPU media {datos['cpu_proceso_pct'].get('media')} %, "
          f"{datos['frames_por_s_logrados']} frames/s, {datos['llamadas_atrasadas_pct']} % atrasadas")
    return datos


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--salida", required=True)
    parser.add_argument("--config", default=str(RAIZ / "cameras.yaml"))
    parser.add_argument("--modelo", help="ruta al .onnx (por defecto, el de cameras.yaml)")
    parser.add_argument("--repeticiones", type=int, default=100)
    parser.add_argument("--segundos", type=float, default=60, help="duración de cada prueba de CPU")
    parser.add_argument("--sin-cpu", action="store_true",
                        help="omite la prueba de CPU sostenida (no es medible con el pipeline corriendo; "
                             "latencia y techo siguen sirviendo para comparar modelos entre sí)")
    parser.add_argument("--imgsz", type=int,
                        help="resolución de entrada. Sin esta opción se llama al modelo como lo hace hoy "
                             "inference_worker (sin imgsz): ultralytics usa 640, no la del modelo")
    parser.add_argument("--condicion", default="pipeline detenido",
                        help="condición del equipo durante la medición, queda en el resultado")
    args = parser.parse_args()

    config = yaml.safe_load(open(args.config, encoding="utf-8"))
    ruta_modelo = args.modelo or str(RAIZ / config["inference"]["model_path"])

    print("Capturando frames...")
    frames, origen = capturar_frames(config)
    print(f"Cargando modelo {ruta_modelo}...")
    modelo = cargar_modelo(ruta_modelo)
    # El log de ultralytics dice "CPUExecutionProvider" aunque corra en GPU
    # (ver model_loader._loguear_proveedor_real): se guarda el proveedor real.
    proveedores = modelo.predictor.model.session.get_providers()
    print(f"Proveedores de la sesión ONNX: {proveedores}")

    if args.imgsz:
        predict_original = modelo.predict

        def predict_con_imgsz(*a, **k):
            return predict_original(*a, imgsz=args.imgsz, **k)

        modelo.predict = predict_con_imgsz

    # Forma real del tensor que entra a la sesión ONNX: la única fuente que no
    # depende de lo que diga la configuración.
    sesion = modelo.predictor.model.session
    formas = []
    run_original = sesion.run

    def run_espia(salidas, entradas, *a, **k):
        if not formas:
            formas.extend(tuple(v.shape) for v in entradas.values())
        return run_original(salidas, entradas, *a, **k)

    sesion.run = run_espia
    modelo.predict(source=frames[:1], conf=CONF, iou=IOU, verbose=False)
    sesion.run = run_original
    print(f"Tensor de entrada para un frame de {frames[0].shape[1]}x{frames[0].shape[0]}: {formas[0]}")

    print("1) Latencia por tamaño de lote")
    latencias = latencia_por_lote(modelo, frames, args.repeticiones)
    print("2) Techo de rendimiento")
    techo = {tamanio: techo_rendimiento(modelo, frames, tamanio, 30) for tamanio in (1, 4)}
    cpu = None
    if not args.sin_cpu:
        print("3) CPU sostenida, peor caso sin filtro de movimiento")
        cpu = {
            "1_camara": cpu_a_ritmo_fijo(modelo, frames, 5, 1, args.segundos),
            "4_camaras_sin_agrupar": cpu_a_ritmo_fijo(modelo, frames, 20, 1, args.segundos),
            "4_camaras_agrupadas": cpu_a_ritmo_fijo(modelo, frames, 5, 4, args.segundos),
        }

    informe = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "condicion": args.condicion,
        "imgsz_pedido": args.imgsz or "sin especificar (como el pipeline: 640 por defecto de ultralytics)",
        "tensor_entrada_primer_frame": list(formas[0]),
        "equipo": {
            "procesador": nombre_procesador(),
            "nucleos_logicos": psutil.cpu_count(logical=True),
            "memoria_gb": round(psutil.virtual_memory().total / 2**30, 1),
        },
        "modelo": {"ruta": ruta_modelo, "bytes": os.path.getsize(ruta_modelo), "proveedores_onnx": proveedores},
        "frames": {"origen": origen, "resoluciones": sorted({f"{f.shape[1]}x{f.shape[0]}" for f in frames})},
        "conf": CONF,
        "iou": IOU,
        "latencia_por_lote": latencias,
        "techo_frames_por_s": techo,
        "cpu_peor_caso": cpu,
    }
    salida = Path(args.salida).with_suffix(".json")
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Resultados en {salida}")


if __name__ == "__main__":
    main()
