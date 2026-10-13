"""
Pruebas de upload/upload_worker.py — subida de eventos a la nube.

requests.post se reemplaza por un doble que registra la llamada, así que no
se sale a la red. Se prueba lo que sostiene la garantía de "ningún evento se
pierde": ante cualquier falla el evento sigue pendiente y se reintenta.
"""

import pytest
import requests

from conftest import RespuestaFalsa
from events.local_buffer import LocalBuffer
from upload import upload_worker
from upload.upload_worker import UploadWorker, _asegurar_https

API = "https://api.fisgon.test"


@pytest.fixture
def buffer(tmp_path):
    b = LocalBuffer(tmp_path / "eventos.db")
    yield b
    b.close()


@pytest.fixture
def worker(buffer, monkeypatch):
    monkeypatch.setenv("FISGON_API_URL", API)
    monkeypatch.setenv("FISGON_EDGE_TOKEN", "token-de-prueba")
    return UploadWorker(buffer, config={})


@pytest.fixture
def post(monkeypatch):
    """Doble de requests.post: devuelve `post.respuesta` (o la lanza si es una
    excepción) y guarda cada llamada en `post.llamadas`."""
    class Doble:
        respuesta = RespuestaFalsa(201, {"id": 99})
        llamadas = []

        def __call__(self, url, json=None, headers=None, timeout=None):
            self.llamadas.append({"url": url, "json": json, "headers": headers})
            if isinstance(self.respuesta, Exception):
                raise self.respuesta
            return self.respuesta

    doble = Doble()
    doble.llamadas = []
    monkeypatch.setattr(upload_worker.requests, "post", doble)
    return doble


# --- Regresión: defecto 5.3, redirect HTTP→HTTPS que perdía eventos ---------

def test_asegurar_https_fuerza_el_esquema_en_urls_remotas():
    assert _asegurar_https("http://api.fisgon.app") == "https://api.fisgon.app"


def test_asegurar_https_respeta_https_y_localhost():
    assert _asegurar_https("https://api.fisgon.app") == "https://api.fisgon.app"
    assert _asegurar_https("http://localhost:8080") == "http://localhost:8080"
    assert _asegurar_https("http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_url_http_del_env_termina_en_https(buffer, monkeypatch):
    monkeypatch.setenv("FISGON_API_URL", "http://api.fisgon.app/")
    monkeypatch.setenv("FISGON_EDGE_TOKEN", "t")
    assert UploadWorker(buffer, config={}).base_url == "https://api.fisgon.app"


# --- Regresión: defecto 5.4, contrato real de la API ------------------------

def test_payload_enviado_respeta_el_contrato_de_la_api(worker, buffer, post, evento_confirmado):
    buffer.save_pending(evento_confirmado)
    worker._subir_pendientes()

    llamada = post.llamadas[0]
    assert llamada["url"] == f"{API}/events"
    assert llamada["headers"] == {"X-Edge-Token": "token-de-prueba"}
    assert llamada["json"] == {
        "camera_id": "cam_test",
        "object_class": "pistol",
        "confidence": 0.91,
        "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
        "frames_validated": 3,
        "snapshot_base64": "c25hcHNob3Q=",
    }
    # La API asigna su propio timestamp: mandar el del edge sería un error.
    assert "timestamp" not in llamada["json"]


# --- Garantía de no pérdida -------------------------------------------------

def test_subida_exitosa_marca_el_evento_como_subido(worker, buffer, post, evento_confirmado):
    buffer.save_pending(evento_confirmado)
    worker._subir_pendientes()
    assert buffer.get_pending() == []


@pytest.mark.parametrize("respuesta", [
    requests.ConnectionError("sin red"),
    requests.Timeout("timeout"),
    RespuestaFalsa(500, text="error interno"),
    RespuestaFalsa(403, text="cámara fuera de cupo"),
    RespuestaFalsa(201, {"sin_id": True}),
], ids=["sin-red", "timeout", "error-500", "rechazo-403", "respuesta-sin-id"])
def test_ante_cualquier_falla_el_evento_sigue_pendiente(worker, buffer, post, evento_confirmado, respuesta):
    buffer.save_pending(evento_confirmado)
    post.respuesta = respuesta

    worker._subir_pendientes()

    assert len(buffer.get_pending()) == 1


def test_evento_pendiente_se_sube_al_volver_la_conexion(worker, buffer, post, evento_confirmado):
    buffer.save_pending(evento_confirmado)

    post.respuesta = requests.ConnectionError("sin red")
    worker._subir_pendientes()
    assert len(buffer.get_pending()) == 1

    post.respuesta = RespuestaFalsa(201, {"id": 5})
    worker._subir_pendientes()
    assert buffer.get_pending() == []
    assert len(post.llamadas) == 2


def test_video_se_sube_al_evento_remoto_correcto(worker, buffer, post, evento_confirmado):
    event_id = buffer.save_pending(evento_confirmado, wait_for_video=True)
    buffer.attach_video(event_id, "dmlkZW8=")
    post.respuesta = RespuestaFalsa(201, {"id": 77})
    worker._subir_pendientes()

    post.respuesta = RespuestaFalsa(200, {})
    worker._subir_videos_pendientes()

    assert post.llamadas[-1]["url"] == f"{API}/events/77/video"
    assert buffer.get_pending_videos() == []


def test_sin_url_configurada_no_intenta_subir(buffer, post, monkeypatch, evento_confirmado):
    monkeypatch.delenv("FISGON_API_URL", raising=False)
    monkeypatch.delenv("FISGON_EDGE_TOKEN", raising=False)
    buffer.save_pending(evento_confirmado)

    UploadWorker(buffer, config={})._subir_pendientes()

    assert post.llamadas == []
    assert len(buffer.get_pending()) == 1
