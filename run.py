"""
run.py
Levanta todo el sistema edge con un solo comando: el pipeline principal
(main.py: cámaras + inferencia + eventos + subida) y la interfaz web de
configuración (config_ui/app.py, http://localhost:8000).

Corren como dos procesos separados a propósito (config_ui no sincroniza en
caliente con el pipeline: los cambios a cameras.yaml se aplican recién
cuando main.py vuelve a arrancar), pero este script los levanta y los
cierra juntos para no tener que abrir dos terminales.

Reinicio automático al guardar: cuando se guarda un cambio en la interfaz
web, config_ui deja el archivo .reiniciar_pipeline y este script reinicia
main.py solo (la interfaz sigue arriba, no se corta la sesión), de modo que
la configuración nueva se aplique sin tener que reiniciar a mano.

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
import signal
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

# Señal que deja config_ui al guardar un cambio de configuración.
FLAG_REINICIO = ROOT / ".reiniciar_pipeline"
# Se espera a que la señal deje de moverse antes de actuar, para que varios
# guardados seguidos (cargar tres cámaras) den un solo reinicio y no tres.
REINICIO_DEBOUNCE_SEGUNDOS = 2


def parsear_args():
    parser = argparse.ArgumentParser(description="Levanta el pipeline edge + la interfaz web de configuración")
    parser.add_argument("--config", type=str, default="cameras.yaml", help="Ruta a cameras.yaml")
    parser.add_argument("--sin-ui", action="store_true", help="No levantar la interfaz web, solo el pipeline")
    return parser.parse_args()


def _lanzar(cmd: list, cwd: Path) -> subprocess.Popen:
    # Grupo de procesos propio: es lo que después permite señalizar al hijo y a
    # todo lo que haya lanzado él, de una (ver _terminar).
    banderas = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        creationflags=banderas,
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


def _reinicio_pedido() -> bool:
    """True si la interfaz pidió un reinicio y ya pasó el tiempo de calma."""
    try:
        antiguedad = time.time() - FLAG_REINICIO.stat().st_mtime
    except OSError:
        return False  # no existe, o desapareció entre el stat y ahora
    return antiguedad >= REINICIO_DEBOUNCE_SEGUNDOS


def _terminar(proc: subprocess.Popen, nombre: str, espera: int = 10):
    """Baja el proceso y todo lo que cuelgue de él.

    En Windows el python del venv es un stub que lanza el intérprete real como
    proceso hijo. Un proc.terminate() mata solo al stub y deja al intérprete
    corriendo huérfano: un pipeline fantasma que sigue subiendo eventos con la
    configuración (y el token) que tenía cargados, en paralelo con el que se
    acaba de levantar. Se ve en el log como líneas duplicadas y respuestas
    contradictorias de la API para el mismo evento.

    Por eso se señaliza al grupo entero con CTRL_BREAK, que Python recibe como
    KeyboardInterrupt y deja cerrar la base y soltar las cámaras. Si algo queda
    vivo igual, recién ahí se baja el árbol a la fuerza.
    """
    if proc.poll() is not None:
        return

    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except OSError:
        pass  # ya se había muerto entre el poll y la señal

    try:
        proc.wait(timeout=espera)
        return
    except subprocess.TimeoutExpired:
        print(f"[run] {nombre} no cerró a tiempo, forzando...")

    if os.name == "nt":
        # /T incluye a los descendientes, que es justamente lo que kill() no ve.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print(f"[run] {nombre} sigue vivo después de forzar el cierre")


def _detener(procesos: dict):
    print("\n[run] Deteniendo procesos...")
    for nombre, proc in procesos.items():
        _terminar(proc, nombre)
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

    def reiniciar_pipeline():
        """Baja y vuelve a levantar main.py para que tome la config nueva.

        No cuenta contra el presupuesto de reintentos por crash: esto es un
        reinicio pedido a propósito, no un proceso que se está cayendo.
        """
        _terminar(procesos["pipeline"], "pipeline")
        lanzar_y_seguir("pipeline")

    # Una señal que haya quedado de una corrida anterior no aplica a esta:
    # el pipeline está por arrancar y ya va a leer la config actual.
    FLAG_REINICIO.unlink(missing_ok=True)

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

            # Se atiende antes que la detección de caídas: al reiniciar acá
            # queda un proceso vivo en procesos["pipeline"], así que el
            # chequeo de abajo no lo confunde con un crash.
            if _reinicio_pedido():
                FLAG_REINICIO.unlink(missing_ok=True)
                print("[run] Cambio guardado en la interfaz — reiniciando el pipeline para aplicarlo...")
                reiniciar_pipeline()
                print("[run] Pipeline reiniciado con la configuración nueva.")

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
