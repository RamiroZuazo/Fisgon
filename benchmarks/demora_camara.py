"""
benchmarks/demora_camara.py
Mide la demora de una cámara: cuánto pasa entre que algo ocurre frente al lente
y que el frame llega a la PC. El pipeline no puede verlo desde adentro, porque
su primera marca de tiempo es la captura.

Procedimiento: con benchmarks/reloj_pc.py corriendo, se sostiene frente a la
cámara (a 30-50 cm) un celular que muestra la hora de ESTA PC al milisegundo.
Este script abre su propia conexión RTSP, lee frames como lo hace
camera_source.py y guarda cada N frames la imagen con la hora de la PC en que
se leyó. Después, para cada imagen: demora = hora de lectura − hora que se ve
en el reloj del celular.

Como ambas horas salen del mismo reloj (el de la PC), no hay error de
sincronización entre equipos; el error es el del reloj de la página (lo muestra
en pantalla, unos pocos ms en la red local) más la mitad del período de
refresco de la pantalla del celular.

La conexión RTSP es independiente de la del pipeline, que sigue funcionando:
las cámaras IP admiten varias sesiones simultáneas.

Uso:
    python benchmarks/demora_camara.py --camara cam_3 --segundos 25 --salida benchmarks/resultados/c2_demora_camara
"""

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import yaml

RAIZ = Path(__file__).parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--camara", required=True, help="id de la cámara en cameras.yaml")
    parser.add_argument("--segundos", type=float, default=25)
    parser.add_argument("--cada", type=int, default=5, help="guardar uno de cada N frames")
    parser.add_argument("--salida", required=True, help="carpeta de salida")
    args = parser.parse_args()

    config = yaml.safe_load(open(RAIZ / "cameras.yaml", encoding="utf-8"))
    camara = next((c for c in config["cameras"] if c["id"] == args.camara), None)
    if camara is None:
        sys.exit(f"No existe la cámara {args.camara} en cameras.yaml")

    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(camara["rtsp_url"])
    if not cap.isOpened():
        sys.exit("No se pudo abrir el flujo RTSP")

    # Se descartan los primeros segundos: al conectar, el decodificador entrega
    # de golpe frames acumulados que no representan la demora en régimen.
    t_fin_descarte = time.monotonic() + 3
    while time.monotonic() < t_fin_descarte:
        cap.read()

    filas = []
    leidos = 0
    t_fin = time.monotonic() + args.segundos
    print(f"Grabando {args.camara} durante {args.segundos:.0f} s...")
    while time.monotonic() < t_fin:
        ok, frame = cap.read()
        hora_lectura = time.time()  # inmediatamente después de leer, como camera_source
        if not ok:
            continue
        leidos += 1
        if leidos % args.cada:
            continue
        legible = datetime.fromtimestamp(hora_lectura).strftime("%H%M%S_%f")[:-3]
        nombre = f"frame_{len(filas):03d}_{legible}.jpg"
        cv2.imwrite(str(salida / nombre), frame)
        filas.append({
            "archivo": nombre,
            "hora_lectura_pc": datetime.fromtimestamp(hora_lectura).strftime("%H:%M:%S.%f")[:-3],
            "hora_reloj_en_imagen": "",  # se completa leyendo la imagen
            "demora_ms": "",
        })
    cap.release()

    with open(salida / "frames.csv", "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=list(filas[0]))
        escritor.writeheader()
        escritor.writerows(filas)
    print(f"{len(filas)} frames guardados de {leidos} leídos en {salida}")


if __name__ == "__main__":
    main()
