"""
benchmarks/analizar_validacion.py
Recall por evento y falsas alarmas de la validación funcional (bloque G), para
varios umbrales de confianza, a partir de una sola sesión.

Entradas:
- el registro de detecciones del pipeline (registro_detecciones en
  cameras.yaml): toda detección con confianza >= 0,10, con la hora UTC de
  captura del frame;
- el registro de ensayos de benchmarks/protocolo_validacion.py: cuándo empezó
  y terminó cada fase en que el arma estuvo a la vista, con el reloj de la PC.

Para cada umbral, las detecciones que lo superan se pasan por la MISMA clase
EventValidator del edge (2 detecciones de la misma cámara y clase en 5 s), así
que los eventos simulados son los que el sistema habría confirmado con ese
umbral. Un ensayo cuenta como detectado si se confirmó al menos un evento cuyo
frame confirmante se capturó dentro de su ventana (desde que empieza la fase
MOSTRÁ hasta 1,5 s después de que termina, margen por la demora de la cámara).

Falsas alarmas: eventos confirmados fuera de toda ventana durante la sesión,
en la cámara de prueba y en las demás cámaras (donde no hubo armas).

Con --clase, un ensayo solo cuenta como detectado si el evento es de esa
clase; un evento de otra clase dentro de la ventana (p. ej. "pistol" mientras
se muestra un cuchillo) se informa aparte como confusión de clase: alertó,
pero con la clase equivocada.

Uso:
    python benchmarks/analizar_validacion.py --detecciones logs/detecciones_validacion.csv \
        --ensayos benchmarks/resultados/g_ensayos.csv --camara cam_3 --salida benchmarks/resultados/g_resultados
    python benchmarks/analizar_validacion.py --detecciones logs/detecciones_validacion.csv \
        --ensayos benchmarks/resultados/g_ensayos_cuchillo.csv --camara cam_4 --clase knife \
        --salida benchmarks/resultados/g_resultados_cuchillo
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from events.event_validator import EventValidator  # noqa: E402

UMBRALES = (0.20, 0.30, 0.35, 0.40, 0.50, 0.55, 0.60, 0.70)
MARGEN_FIN = 1.5  # se puede cambiar con --margen
FRAME = np.zeros((8, 8, 3), dtype="uint8")


def cargar_ensayos(ruta):
    """Ensayos de la primera sesión completa: se corta en la primera marca
    sesion_terminada, para que un reinicio posterior de la página (que vuelve
    a registrar el ensayo 1) no pise los horarios reales."""
    ensayos = {}
    for f in csv.DictReader(open(ruta, encoding="utf-8")):
        if f["evento"] == "sesion_terminada":
            break
        if f["evento"] in ("inicio", "fin"):
            e = ensayos.setdefault(int(f["n"]), {k: f[k] for k in ("distancia", "angulo", "luz", "repeticion")})
            e[f["evento"]] = float(f["hora_utc"])
    return dict(sorted(ensayos.items()))


def cargar_detecciones(ruta, desde, hasta):
    filas = []
    for f in csv.DictReader(open(ruta, encoding="utf-8")):
        t = float(f["captura_utc"])
        if desde <= t <= hasta:
            filas.append({"utc": t, "mono": float(f["captura_monotonic"]), "camera_id": f["camera_id"],
                          "object_class": f["object_class"], "confidence": float(f["confidence"]),
                          "bbox": [float(f[k]) for k in ("x1", "y1", "x2", "y2")]})
    return sorted(filas, key=lambda d: d["mono"])


def simular(detecciones, umbral, config_validacion):
    """Eventos que el sistema habría confirmado con este umbral."""
    eventos = []
    validador = EventValidator({"event_validation": config_validacion}, on_event_confirmed=eventos.append)
    utc_de = {}
    for d in detecciones:
        if d["confidence"] < umbral:
            continue
        utc_de[d["mono"]] = d["utc"]
        validador.procesar_deteccion({
            "camera_id": d["camera_id"], "object_class": d["object_class"], "confidence": d["confidence"],
            "bbox": d["bbox"], "frame": FRAME, "timestamp": d["mono"], "t_inferencia": d["mono"],
        })
    return [{"camera_id": e["camera_id"], "object_class": e["object_class"],
             "utc": utc_de[e["detection_monotonic"]], "frames_validated": e["frames_validated"]} for e in eventos]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--detecciones", required=True)
    parser.add_argument("--ensayos", required=True)
    parser.add_argument("--camara", required=True)
    parser.add_argument("--salida", required=True)
    parser.add_argument("--min-detecciones", type=int, default=2)
    parser.add_argument("--ventana", type=float, default=5)
    parser.add_argument("--margen", type=float, default=MARGEN_FIN,
                        help="segundos después del fin de la fase MOSTRÁ que todavía cuentan para el ensayo")
    parser.add_argument("--clase", help="clase esperada (pistol o knife); sin esto vale cualquier clase")
    args = parser.parse_args()
    margen = args.margen

    ensayos = cargar_ensayos(args.ensayos)
    inicio_sesion = min(e["inicio"] for e in ensayos.values()) - 15
    fin_sesion = max(e["fin"] for e in ensayos.values()) + 10
    detecciones = cargar_detecciones(args.detecciones, inicio_sesion, fin_sesion)
    config_validacion = {"min_consecutive_detections": args.min_detecciones, "window_seconds": args.ventana}

    def ventana_de(utc):
        for n, e in ensayos.items():
            if e["inicio"] <= utc <= e["fin"] + margen:
                return n
        return None

    # Detecciones individuales (frames) de la cámara de prueba dentro de cada ventana.
    frames_por_ensayo = defaultdict(list)
    for d in detecciones:
        if d["camera_id"] == args.camara:
            n = ventana_de(d["utc"])
            if n is not None:
                frames_por_ensayo[n].append(d)

    resultados = {"sesion": {
        "ensayos": len(ensayos), "camara": args.camara,
        "duracion_s": round(fin_sesion - inicio_sesion, 1),
        "detecciones_registradas_en_la_sesion": len(detecciones),
        "validador": config_validacion, "margen_fin_ventana_s": margen, "clase_esperada": args.clase,
    }, "por_umbral": {}, "por_ensayo": {}}

    for umbral in UMBRALES:
        eventos = simular(detecciones, umbral, config_validacion)
        detectados, clases, primeras, falsas = {}, defaultdict(int), {}, []
        confusiones = defaultdict(list)
        for ev in eventos:
            n = ventana_de(ev["utc"]) if ev["camera_id"] == args.camara else None
            if n is None:
                falsas.append(ev)
            elif args.clase and ev["object_class"] != args.clase:
                confusiones[n].append(ev["object_class"])
            else:
                detectados.setdefault(n, ev)
                primeras.setdefault(n, ev["utc"] - ensayos[n]["inicio"])
        for n, ev in detectados.items():
            clases[ev["object_class"]] += 1
        por_condicion = defaultdict(lambda: [0, 0])
        for n, e in ensayos.items():
            for clave in (f"distancia: {e['distancia']}", f"angulo: {e['angulo']}", f"luz: {e['luz']}"):
                por_condicion[clave][1] += 1
                por_condicion[clave][0] += n in detectados
        resultados["por_umbral"][f"{umbral:.2f}"] = {
            "ensayos_detectados": len(detectados),
            "recall_por_evento": round(len(detectados) / len(ensayos), 3),
            "no_detectados": [n for n in ensayos if n not in detectados],
            "clase_del_primer_evento": dict(clases),
            "confusiones_de_clase": {n: sorted(set(c)) for n, c in sorted(confusiones.items())},
            "ensayos_solo_con_clase_equivocada": [n for n in sorted(confusiones) if n not in detectados],
            "segundos_hasta_el_frame_confirmante": sorted(round(s, 2) for s in primeras.values()),
            "falsas_alarmas_en_la_sesion": len(falsas),
            "falsas_alarmas_detalle": [{"camera_id": f["camera_id"], "object_class": f["object_class"],
                                        "segundos_desde_inicio_sesion": round(f["utc"] - inicio_sesion, 1)}
                                       for f in falsas],
            "por_condicion": {k: f"{v[0]}/{v[1]}" for k, v in sorted(por_condicion.items())},
        }
        print(f"umbral {umbral:.2f}: {len(detectados)}/{len(ensayos)} ensayos detectados "
              f"({len(detectados) / len(ensayos):.0%}), falsas alarmas {len(falsas)}, "
              f"no detectados {[n for n in ensayos if n not in detectados]}, clases {dict(clases)}"
              + (f", confusiones de clase en {sorted(confusiones)}" if confusiones else ""))

    for n, e in ensayos.items():
        frames = frames_por_ensayo.get(n, [])
        resultados["por_ensayo"][n] = {
            **{k: e[k] for k in ("distancia", "angulo", "luz", "repeticion")},
            "frames_con_deteccion_en_la_ventana": len(frames),
            "confianza_maxima": round(max((f["confidence"] for f in frames), default=0), 3),
            "clases_detectadas": dict(sorted({c: sum(1 for f in frames if f["object_class"] == c)
                                              for c in {f["object_class"] for f in frames}}.items())),
        }

    salida = Path(args.salida).with_suffix(".json")
    salida.write_text(json.dumps(resultados, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Resultados en {salida}")


if __name__ == "__main__":
    main()
