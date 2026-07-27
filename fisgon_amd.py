"""
Detección en tiempo real con YOLOv8 usando webcam — GPU AMD (DirectML via ONNX Runtime)
Requiere: pip install ultralytics opencv-python onnxruntime-directml
Uso: python fisgon_amd.py [--modelo best.pt] [--camara 0] [--confianza 0.5]

Si se pasa un .pt, se exporta automáticamente a .onnx la primera vez.
"""

import cv2
import argparse
import sys
from pathlib import Path


def parsear_args():
    parser = argparse.ArgumentParser(description="Detección en tiempo real con YOLO (GPU AMD via DirectML)")
    parser.add_argument("--modelo", type=str, default="best.pt",
                        help="Ruta al modelo YOLO .pt o .onnx (default: best.pt)")
    parser.add_argument("--camara", type=int, default=0,
                        help="Índice de cámara (default: 0 = webcam integrada)")
    parser.add_argument("--confianza", type=float, default=0.5,
                        help="Umbral de confianza (default: 0.5)")
    parser.add_argument("--iou", type=float, default=0.45,
                        help="Umbral IOU para NMS (default: 0.45)")
    parser.add_argument("--ancho", type=int, default=1280,
                        help="Ancho del frame de captura (default: 1280)")
    parser.add_argument("--alto", type=int, default=720,
                        help="Alto del frame de captura (default: 720)")
    parser.add_argument("--sin-etiquetas", action="store_true",
                        help="Ocultar etiquetas de clase")
    parser.add_argument("--sin-confianza", action="store_true",
                        help="Ocultar valores de confianza")
    parser.add_argument("--fps-max", type=float, default=0,
                        help="Límite de FPS de captura (0 = sin límite, ej: 5)")
    return parser.parse_args()


def activar_directml():
    """Parchea onnxruntime para forzar DmlExecutionProvider (AMD GPU)."""
    try:
        import onnxruntime as _ort
        available = _ort.get_available_providers()
        if "DmlExecutionProvider" not in available:
            print("[WARN] DmlExecutionProvider no disponible. Instalá onnxruntime-directml.")
            return False

        _SessionOriginal = _ort.InferenceSession

        class _SesionDirectML(_SessionOriginal):
            def __init__(self, path, sess_options=None, providers=None, **kwargs):
                providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
                super().__init__(path, sess_options, providers, **kwargs)

        _ort.InferenceSession = _SesionDirectML
        print("[INFO] GPU AMD activada via DirectML (ONNX Runtime)")
        return True
    except ImportError:
        print("[WARN] onnxruntime-directml no está instalado. Usando CPU.")
        print("       Instalalo con: pip install onnxruntime-directml")
        return False
    except Exception as e:
        print(f"[WARN] Error activando DirectML: {e}. Usando CPU.")
        return False


def resolver_modelo(ruta_modelo: Path) -> Path:
    """Si es .pt, busca el .onnx exportado al lado. Si no existe, indica cómo generarlo."""
    if ruta_modelo.suffix.lower() == ".onnx":
        return ruta_modelo

    ruta_onnx = ruta_modelo.with_suffix(".onnx")
    if ruta_onnx.exists():
        print(f"[INFO] Usando modelo ONNX: {ruta_onnx}")
        return ruta_onnx

    print(f"[ERROR] No se encontró el modelo ONNX: {ruta_onnx}")
    print(f"        Exportalo primero con:")
    print(f"        python -c \"from ultralytics import YOLO; YOLO('{ruta_modelo}').export(format='onnx')\"")
    sys.exit(1)


def main():
    args = parsear_args()

    ruta_modelo = Path(args.modelo)
    if not ruta_modelo.exists():
        print(f"[ERROR] No se encontró el modelo: {ruta_modelo.resolve()}")
        sys.exit(1)

    try:
        from ultralytics import YOLO
    except ImportError:
        print("[ERROR] ultralytics no está instalado.")
        print("        Instalalo con: pip install ultralytics")
        sys.exit(1)

    # Activar DirectML antes de cargar cualquier modelo ONNX
    activar_directml()

    # Exportar a ONNX si hace falta
    ruta_onnx = resolver_modelo(ruta_modelo)

    print(f"[INFO] Cargando modelo: {ruta_onnx}")
    modelo = YOLO(str(ruta_onnx))

    print(f"[INFO] Abriendo cámara {args.camara}...")
    cap = cv2.VideoCapture(args.camara)
    if not cap.isOpened():
        print(f"[ERROR] No se pudo abrir la cámara {args.camara}.")
        print("        Probá con --camara 1 si tenés varias cámaras.")
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.ancho)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.alto)

    ancho_real = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    alto_real = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[INFO] Resolución: {ancho_real}x{alto_real}")
    print("[INFO] Presioná 'q' o ESC para salir | 's' para capturar pantalla")
    print()

    contador_fps = 0
    fps = 0.
    timer = cv2.getTickCount()
    captura_num = 0
    ultimo_frame = cv2.getTickCount()
    intervalo_min = (1.0 / args.fps_max) * cv2.getTickFrequency() if args.fps_max > 0 else 0

    fallos_consecutivos = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            fallos_consecutivos += 1
            print(f"[WARN] No se pudo leer el frame ({fallos_consecutivos})...")
            if fallos_consecutivos >= 10:
                print("[ERROR] Demasiados fallos consecutivos. Cerrando.")
                break
            continue
        fallos_consecutivos = 0

        if intervalo_min > 0:
            ahora = cv2.getTickCount()
            if (ahora - ultimo_frame) < intervalo_min:
                cv2.waitKey(1)
                continue
            ultimo_frame = ahora

        resultados = modelo.predict(
            source=frame,
            conf=args.confianza,
            iou=args.iou,
            verbose=False
        )

        frame_anotado = resultados[0].plot(
            labels=not args.sin_etiquetas,
            conf=not args.sin_confianza
        )

        contador_fps += 1
        if contador_fps >= 10:
            tiempo = (cv2.getTickCount() - timer) / cv2.getTickFrequency()
            fps = contador_fps / tiempo
            contador_fps = 0
            timer = cv2.getTickCount()

        n_detecciones = len(resultados[0].boxes)
        cv2.rectangle(frame_anotado, (0, 0), (260, 70), (0, 0, 0), -1)
        cv2.putText(frame_anotado, f"FPS: {fps:.1f}", (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(frame_anotado, f"Detecciones: {n_detecciones}", (10, 55),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

        cv2.imshow("Deteccion YOLO - AMD DirectML  [Q/ESC=salir | S=captura]", frame_anotado)

        tecla = cv2.waitKey(1) & 0xFF
        if tecla in (ord("q"), 27):
            break
        elif tecla == ord("s"):
            captura_num += 1
            nombre = f"captura_{captura_num:03d}.jpg"
            cv2.imwrite(nombre, frame_anotado)
            print(f"[INFO] Captura guardada: {nombre}")

    cap.release()
    cv2.destroyAllWindows()
    print("[INFO] Cerrando. ¡Hasta luego!")


if __name__ == "__main__":
    main()
