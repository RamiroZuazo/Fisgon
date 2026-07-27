"""
run.py
Levanta todo el sistema edge con un solo comando: el pipeline principal
(main.py: cámaras + inferencia + eventos + subida) y la interfaz web de
configuración (config_ui/app.py, http://localhost:8000).

Corren como dos procesos separados a propósito (ver CONTEXT.md: config_ui no
sincroniza en caliente con el pipeline — los cambios a cameras.yaml se
aplican recién cuando se reinicia), pero este script los levanta y los
cierra juntos para no tener que abrir dos terminales.

Watchdog: pensado para correr 24/7 sin supervisión. Si cualquiera de los dos
procesos se cae solo (crash, excepción no capturada, driver que se cuelga),
run.py lo reinicia automáticamente en unos segundos — no hace falta que
alguien lo note y lo reinicie a mano. Si un proceso se cae demasiadas veces
seguidas (5 veces en 10 minutos), se asume que el problema no se va a
resolver solo reintentando, y run.py se detiene del todo en vez de quedar
reiniciando en loop indefinidamente.

Uso:
    python run.py --config cameras.yaml
    python run.py --sin-ui          # solo el pipeline, sin la interfaz web
    Ctrl+C cierra ambos procesos.
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent

# La consola de Windows suele usar cp1252/cp850, no UTF-8. Se fuerza UTF-8 en
# los subprocesos (para que emitan bytes UTF-8 reales) y se reconfigura la
# salida de este proceso para poder imprimirlos sin UnicodeEncodeError.
_ENV_HIJOS = {**os.environ, "PYTHONIOENCODING": "utf-8"}
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

CHEQUEO_INTERVALO_SEGUNDOS = 1
REINICIO_DELAY_SEGUNDOS = 5
MAX_REINICIOS_EN_VENTANA = 5
VENTANA_REINICIOS_SEGUNDOS = 600  # 10 minutos


def parsear_args():
    parser = argparse.ArgumentParser(description="Levanta el pipeline edge + la interfaz web de configuración")
    parser.add_argument("--config", type=str, default="cameras.yaml", help="Ruta a cameras.yaml")
    parser.add_argument("--sin-ui", action="store_true", help="No levantar la interfaz web, solo el pipeline")
    return parser.parse_args()


def _lanzar(cmd: list, cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_ENV_HIJOS,
    )


def _reenviar_salida(proceso: subprocess.Popen, prefijo: str):
    """Reenvía la salida del subproceso a esta consola, con un prefijo para saber quién habla."""
    for linea in iter(proceso.stdout.readline, ""):
        if not linea:
            break
        print(f"[{prefijo}] {linea}", end="")


def _detener(procesos: dict):
    print("\n[run] Deteniendo procesos...")
    for nombre, proc in procesos.items():
        if proc.poll() is None:
            proc.terminate()
    for nombre, proc in procesos.items():
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"[run] {nombre} no cerró a tiempo, forzando...")
            proc.kill()
    print("[run] Listo.")


def main():
    args = parsear_args()

    especificaciones = {"pipeline": [sys.executable, "main.py", "--config", args.config]}
    if not args.sin_ui:
        especificaciones["config_ui"] = [sys.executable, "config_ui/app.py"]

    procesos = {}
    reinicios_recientes = {nombre: [] for nombre in especificaciones}

    def lanzar_y_seguir(nombre):
        proc = _lanzar(especificaciones[nombre], ROOT)
        procesos[nombre] = proc
        threading.Thread(target=_reenviar_salida, args=(proc, nombre), daemon=True).start()

    for nombre in especificaciones:
        lanzar_y_seguir(nombre)

    print("=== Sistema edge levantado ===")
    print("Pipeline: cámaras + inferencia + eventos + subida")
    if not args.sin_ui:
        print("Config UI: http://localhost:8000")
    print("Ctrl+C para detener todo.\n")

    detener_todo = False
    try:
        while not detener_todo:
            time.sleep(CHEQUEO_INTERVALO_SEGUNDOS)
            for nombre in list(especificaciones):
                proc = procesos[nombre]
                if proc.poll() is None:
                    continue  # sigue vivo

                codigo = proc.returncode
                print(f"[run] [{nombre}] se cerró inesperadamente (código {codigo})")

                ahora = time.monotonic()
                reinicios_recientes[nombre] = [
                    t for t in reinicios_recientes[nombre] if ahora - t < VENTANA_REINICIOS_SEGUNDOS
                ]
                if len(reinicios_recientes[nombre]) >= MAX_REINICIOS_EN_VENTANA:
                    print(
                        f"[run] [{nombre}] se cayó {MAX_REINICIOS_EN_VENTANA} veces en menos de "
                        f"{VENTANA_REINICIOS_SEGUNDOS // 60} minutos — dejo de reintentar. "
                        f"Revisar logs/edge.log para ver la causa."
                    )
                    detener_todo = True
                    break

                reinicios_recientes[nombre].append(ahora)
                print(f"[run] [{nombre}] reintentando en {REINICIO_DELAY_SEGUNDOS}s...")
                time.sleep(REINICIO_DELAY_SEGUNDOS)
                lanzar_y_seguir(nombre)
    except KeyboardInterrupt:
        pass
    finally:
        _detener(procesos)


if __name__ == "__main__":
    main()
