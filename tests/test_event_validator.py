"""
Pruebas de events/event_validator.py — RF003, validación temporal.

Es el mecanismo central de reducción de falsos positivos: un evento solo se
confirma con `min_consecutive_detections` detecciones de la misma cámara y
clase dentro de `window_seconds`. Los timestamps se inyectan a mano, así que
las pruebas no dependen del reloj ni de la velocidad de la máquina.
"""

import base64

import cv2
import numpy as np
import pytest

from events.event_validator import EventValidator

CONFIG = {"event_validation": {"min_consecutive_detections": 3, "window_seconds": 5}}


@pytest.fixture
def eventos():
    return []


@pytest.fixture
def validador(eventos):
    return EventValidator(CONFIG, on_event_confirmed=eventos.append)


@pytest.fixture
def detectar(validador, frame):
    def _detectar(ts, camera_id="cam_1", object_class="pistol", bbox=(10, 20, 40, 60), confidence=0.9):
        validador.procesar_deteccion({
            "camera_id": camera_id,
            "object_class": object_class,
            "confidence": confidence,
            "bbox": list(bbox),
            "frame": frame,
            "timestamp": ts,
        })
    return _detectar


def test_una_sola_deteccion_no_confirma(detectar, eventos):
    detectar(100.0)
    assert eventos == []


def test_n_detecciones_dentro_de_la_ventana_confirman(detectar, eventos):
    for ts in (100.0, 100.2, 100.4):
        detectar(ts)
    assert len(eventos) == 1
    assert eventos[0]["frames_validated"] == 3


def test_n_detecciones_fuera_de_la_ventana_no_confirman(detectar, eventos):
    # Tres detecciones, pero nunca tres juntas dentro de 5 segundos.
    for ts in (100.0, 103.0, 106.5):
        detectar(ts)
    assert eventos == []


def test_clases_distintas_no_se_acumulan(detectar, eventos):
    detectar(100.0, object_class="pistol")
    detectar(100.2, object_class="knife")
    detectar(100.4, object_class="pistol")
    assert eventos == []


def test_camaras_distintas_no_se_acumulan(detectar, eventos):
    detectar(100.0, camera_id="cam_1")
    detectar(100.2, camera_id="cam_2")
    detectar(100.4, camera_id="cam_1")
    assert eventos == []


def test_racha_continua_no_genera_eventos_duplicados(detectar, eventos):
    # El arma sigue en cuadro a 5 FPS: un solo evento mientras no pase la ventana.
    for i in range(20):
        detectar(100.0 + i * 0.2)
    assert len(eventos) == 1


def test_pasada_la_ventana_vuelve_a_confirmar(detectar, eventos):
    for ts in (100.0, 100.2, 100.4):
        detectar(ts)
    for ts in (106.0, 106.2, 106.4):
        detectar(ts)
    assert len(eventos) == 2


def test_bbox_se_convierte_a_xywh():
    assert EventValidator._bbox_a_xywh([10, 20, 40, 60]) == {"x": 10, "y": 20, "width": 30, "height": 40}


def test_payload_confirmado_tiene_el_contrato_de_la_api(detectar, eventos):
    """Regresión del defecto 5.4 de SISTEMA.md: el contrato real de la API
    exige frames_validated y el bbox como objeto, no como lista."""
    for ts in (100.0, 100.2, 100.4):
        detectar(ts, bbox=(1, 2, 11, 22), confidence=0.87)

    payload = eventos[0]
    assert payload["camera_id"] == "cam_1"
    assert payload["object_class"] == "pistol"
    assert payload["confidence"] == 0.87
    assert payload["bbox"] == {"x": 1, "y": 2, "width": 10, "height": 20}
    assert payload["frames_validated"] == 3
    assert payload["detection_monotonic"] == 100.4


def test_snapshot_es_un_jpeg_valido(detectar, eventos):
    for ts in (100.0, 100.2, 100.4):
        detectar(ts)

    jpg = base64.b64decode(eventos[0]["snapshot_base64"])
    assert jpg[:2] == b"\xff\xd8"  # marca de inicio JPEG
    imagen = cv2.imdecode(np.frombuffer(jpg, dtype="uint8"), cv2.IMREAD_COLOR)
    assert imagen is not None and imagen.shape == (48, 64, 3)
