"""
Pruebas de la política de la cola de frames (SISTEMA.md, sección 5.10).

Regresión de un defecto encontrado midiendo: con la cola llena se descartaba
el frame nuevo, y ante un pico de CPU el modelo analizaba imágenes de varios
segundos atrás. Ahora se descarta el más viejo y la cola mide un segundo de
frames.
"""

from queue import Queue

from capture.camera_source import CameraSource
from capture.camera_manager import tamanio_cola


def _fuente(cola, camera_id="cam_1"):
    return CameraSource(camera_id=camera_id, rtsp_url="0", frame_queue=cola)


def test_cola_llena_descarta_el_frame_mas_viejo(frame):
    cola = Queue(maxsize=3)
    fuente = _fuente(cola)
    for ts in (1.0, 2.0, 3.0, 4.0, 5.0):
        fuente._enqueue(frame, ts)

    assert [cola.get_nowait()["timestamp"] for _ in range(cola.qsize())] == [3.0, 4.0, 5.0]


def test_el_frame_mas_nuevo_siempre_entra(frame):
    cola = Queue(maxsize=1)
    _fuente(cola, "cam_1")._enqueue(frame, 1.0)
    _fuente(cola, "cam_2")._enqueue(frame, 2.0)

    item = cola.get_nowait()
    assert (item["camera_id"], item["timestamp"]) == ("cam_2", 2.0)


def test_cola_mide_un_segundo_de_frames():
    config = {
        "inference": {"inference_fps": 5},
        "cameras": [{"id": "a", "active": True}, {"id": "b", "active": True},
                    {"id": "c", "active": False}],
    }
    assert tamanio_cola(config) == 10


def test_sin_camaras_activas_la_cola_no_es_infinita():
    # Queue(maxsize=0) sería una cola sin límite.
    assert tamanio_cola({"cameras": []}) == 1
