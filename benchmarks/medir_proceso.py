"""
benchmarks/medir_proceso.py
Mide el consumo del pipeline mientras corre: CPU y memoria del proceso de
main.py, CPU total del equipo, y cuadros por segundo efectivamente procesados
por el modelo para cada cámara.

No toca el pipeline: lee el proceso desde afuera (psutil) y los contadores de
frames desde logs/edge.log, donde main.py los vuelca cada 5 segundos. Sirve
tanto para el sistema en producción como para una corrida de prueba.

La CPU del proceso se informa de dos formas, porque se presta a confusión:
- cpu_proceso_pct: sobre la capacidad TOTAL del equipo (100 % = los 6 núcleos
  al máximo). Es la cifra que se usa en SISTEMA.md y en el informe.
- cpu_proceso_nucleos: en núcleos equivalentes (1,0 = un núcleo entero).

Uso:
    python benchmarks/medir_proceso.py --duracion 600 --salida resultados/b1_produccion
    python benchmarks/medir_proceso.py --pid 5868 ...   (si hay más de un main.py)

Genera <salida>.csv (una fila por muestra) y <salida>.json (resumen).
"""

import argparse
import ast
import json
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

RAIZ = Path(__file__).parent.parent
LOG = RAIZ / "logs" / "edge.log"
PATRON_FRAMES = re.compile(r"frames procesados por el modelo: (\{.*\})")


def buscar_main() -> psutil.Process:
    """El proceso que corre de verdad main.py. En Windows, el python.exe del
    venv es un lanzador que crea un hijo con el intérprete base: se toma el
    que no tiene otro main.py como hijo."""
    candidatos = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        cmd = " ".join(p.info["cmdline"] or [])
        if "python" in (p.info["name"] or "").lower() and "main.py" in cmd:
            candidatos.append(p)
    hojas = [p for p in candidatos
             if not any("main.py" in " ".join(h.cmdline()) for h in p.children())]
    if len(hojas) != 1:
        sys.exit(f"Se esperaba un único proceso main.py, se encontraron {len(hojas)}: usar --pid")
    return hojas[0]


def ultimo_conteo_frames() -> tuple[float, dict] | None:
    """(timestamp, {cam: frames}) de la última línea de estado del log."""
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(f.tell() - 20000, 0))
            lineas = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for linea in reversed(lineas):
        m = PATRON_FRAMES.search(linea)
        if m:
            ts = datetime.strptime(linea[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            return ts, ast.literal_eval(m.group(1))
    return None


def nombre_procesador() -> str:
    """platform.processor() en Windows da la familia ("Intel64 Family 6..."),
    no el modelo: el nombre comercial está en el registro."""
    try:
        import winreg
        clave_cpu = "\\".join(["HARDWARE", "DESCRIPTION", "System", "CentralProcessor", "0"])
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, clave_cpu) as clave:
            return winreg.QueryValueEx(clave, "ProcessorNameString")[0].strip()
    except OSError:
        import platform
        return platform.processor()


def resumen(valores: list[float]) -> dict:
    if not valores:
        return {}
    ordenados = sorted(valores)
    p95 = ordenados[min(len(ordenados) - 1, round(0.95 * (len(ordenados) - 1)))]
    return {
        "media": round(statistics.mean(valores), 2),
        "mediana": round(statistics.median(valores), 2),
        "p95": round(p95, 2),
        "min": round(min(valores), 2),
        "max": round(max(valores), 2),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duracion", type=float, default=600, help="segundos de medición")
    parser.add_argument("--intervalo", type=float, default=5, help="segundos entre muestras")
    parser.add_argument("--pid", type=int, help="PID de main.py (por defecto se busca solo)")
    parser.add_argument("--salida", required=True, help="ruta base de los archivos de salida")
    parser.add_argument("--descripcion", default="", help="condición medida, queda en el resumen")
    args = parser.parse_args()

    proceso = psutil.Process(args.pid) if args.pid else buscar_main()
    nucleos = psutil.cpu_count(logical=True)
    salida = Path(args.salida)
    salida.parent.mkdir(parents=True, exist_ok=True)

    # La primera lectura de cpu_percent siempre da 0: inicializa el contador.
    proceso.cpu_percent(None)
    psutil.cpu_percent(None)
    inicio_frames = ultimo_conteo_frames()

    muestras = []
    t0 = time.monotonic()
    print(f"Midiendo PID {proceso.pid} durante {args.duracion:.0f}s ({nucleos} núcleos lógicos)...")
    while time.monotonic() - t0 < args.duracion:
        time.sleep(args.intervalo)
        cpu_nucleos = proceso.cpu_percent(None) / 100
        muestras.append({
            "t": round(time.monotonic() - t0, 1),
            "cpu_proceso_pct": round(cpu_nucleos / nucleos * 100, 2),
            "cpu_proceso_nucleos": round(cpu_nucleos, 3),
            "cpu_equipo_pct": psutil.cpu_percent(None),
            "rss_mb": round(proceso.memory_info().rss / 2**20, 1),
        })
    fin_frames = ultimo_conteo_frames()

    with open(salida.with_suffix(".csv"), "w", encoding="utf-8") as f:
        f.write(",".join(muestras[0]) + "\n")
        for m in muestras:
            f.write(",".join(str(v) for v in m.values()) + "\n")

    fps = {}
    if inicio_frames and fin_frames and fin_frames[0] > inicio_frames[0]:
        segundos = fin_frames[0] - inicio_frames[0]
        for cam, total in fin_frames[1].items():
            fps[cam] = round((total - inicio_frames[1].get(cam, 0)) / segundos, 2)

    informe = {
        "descripcion": args.descripcion,
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "equipo": {
            "procesador": nombre_procesador(),
            "nucleos_logicos": nucleos,
            "memoria_gb": round(psutil.virtual_memory().total / 2**30, 1),
        },
        "pid": proceso.pid,
        "duracion_s": args.duracion,
        "muestras": len(muestras),
        "cpu_proceso_pct": resumen([m["cpu_proceso_pct"] for m in muestras]),
        "cpu_equipo_pct": resumen([m["cpu_equipo_pct"] for m in muestras]),
        "rss_mb": resumen([m["rss_mb"] for m in muestras]),
        "fps_inferencia_por_camara": fps,
        "fps_inferencia_total": round(sum(fps.values()), 2) if fps else None,
    }
    salida.with_suffix(".json").write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(informe, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
