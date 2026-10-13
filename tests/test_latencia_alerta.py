"""
Pruebas del camino de la alerta: subida inmediata y medición de latencia
(SISTEMA.md, sección 5.11; RNF001).

Regresión de un defecto encontrado leyendo el código antes de medir: el
upload_worker solo subía eventos en su revisión periódica (cada 10 s), así que
un evento confirmado esperaba hasta 10 s en la base local antes de salir.
"""

import csv
import time

import pytest

from conftest import RespuestaFalsa
from events.event_validator import EventValidator
from events.local_buffer import LocalBuffer
from upload import upload_worker
from upload.upload_worker import ETAPAS, UploadWorker

POLL_LARGO = 30  # revisión periódica mucho más lenta que lo que mide la prueba


@pytest.fixture
def buffer(tmp_path):
    b = LocalBuffer(tmp_path / "eventos.db")
    yield b
    b.close()


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("FISGON_API_URL", "https://api.fisgon.test")
    monkeypatch.setenv("FISGON_EDGE_TOKEN", "token-de-prueba")
    monkeypatch.setattr(upload_worker, "LATENCIA_CSV", tmp_path / "latencia_eventos.csv")
    llamadas = []

    def post(url, json=None, headers=None, timeout=None):
        llamadas.append(time.monotonic())
        return RespuestaFalsa(201, {"id": 500 + len(llamadas)})

    monkeypatch.setattr(upload_worker.requests, "post", post)
    return llamadas


def _marcas_completas():
    base = time.monotonic()
    return {
        "captura_primera": base - 0.6,
        "captura_confirmante": base - 0.4,
        "inferencia": base - 0.3,
        "confirmacion": base - 0.25,
        "guardado": base - 0.2,
    }


def _esperar(condicion, segundos=2.0):
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.02)
    return False


def _arrancar(buffer, config):
    worker = UploadWorker(buffer, config=config, poll_interval_seconds=POLL_LARGO)
    worker.start()
    # Deja pasar la revisión inicial del arranque (buffer vacío).
    time.sleep(0.2)
    return worker


def test_evento_nuevo_se_sube_en_el_acto(buffer, api, evento_confirmado):
    worker = _arrancar(buffer, config={})
    try:
        event_id = buffer.save_pending(evento_confirmado)
        worker.nuevo_evento(event_id, _marcas_completas())

        assert _esperar(lambda: buffer.get_pending() == [], segundos=2.0)
    finally:
        worker.stop()


def test_sin_envio_inmediato_el_evento_espera_la_revision_periodica(buffer, api, evento_confirmado):
    """El comportamiento anterior, que se conserva detrás de la opción para
    poder medirlo: el evento no sale hasta el próximo ciclo."""
    worker = _arrancar(buffer, config={"upload": {"envio_inmediato": False}})
    try:
        event_id = buffer.save_pending(evento_confirmado)
        worker.nuevo_evento(event_id, _marcas_completas())

        assert not _esperar(lambda: buffer.get_pending() == [], segundos=1.0)
        assert api == []
    finally:
        worker.stop()


def test_latencia_queda_registrada_con_todas_las_etapas(buffer, api, evento_confirmado):
    worker = UploadWorker(buffer, config={})
    event_id = buffer.save_pending(evento_confirmado)
    worker.nuevo_evento(event_id, _marcas_completas())

    worker._subir_pendientes()

    with open(upload_worker.LATENCIA_CSV, encoding="utf-8") as f:
        filas = list(csv.DictReader(f))
    assert len(filas) == 1
    fila = filas[0]
    nombres = [etapa for etapa, _ in ETAPAS]
    tramos = [int(fila[f"{a}_a_{b}_ms"]) for a, b in zip(nombres, nombres[1:])]
    assert all(t >= 0 for t in tramos)
    # El total es la suma de los tramos (con tolerancia por redondeo).
    assert abs(sum(tramos) - int(fila["total_ms"])) <= len(tramos)
    assert fila["evento_remoto"] == "501"


def test_evento_sin_marcas_se_sube_igual_y_no_registra_latencia(buffer, api, evento_confirmado):
    """Un evento que quedó pendiente de antes de un reinicio no tiene marcas:
    se sube igual, pero su latencia no se mide."""
    buffer.save_pending(evento_confirmado)
    UploadWorker(buffer, config={})._subir_pendientes()

    assert buffer.get_pending() == []
    assert not upload_worker.LATENCIA_CSV.exists()


def test_validador_entrega_las_marcas_en_orden(frame):
    eventos = []
    validador = EventValidator(
        {"event_validation": {"min_consecutive_detections": 3, "window_seconds": 5}},
        on_event_confirmed=eventos.append,
    )
    for ts in (100.0, 100.2, 100.4):
        validador.procesar_deteccion({
            "camera_id": "cam_1", "object_class": "pistol", "confidence": 0.9,
            "bbox": [0, 0, 10, 10], "frame": frame, "timestamp": ts, "t_inferencia": ts + 0.05,
        })

    marcas = eventos[0]["marcas"]
    assert marcas["captura_primera"] == 100.0  # la primera de la racha, no la última
    assert marcas["captura_confirmante"] == 100.4
    assert marcas["inferencia"] == pytest.approx(100.45)
    assert "confirmacion" in marcas


def test_un_video_lento_no_demora_una_alerta(buffer, monkeypatch, tmp_path, evento_confirmado):
    """Regresión de SISTEMA.md 5.12: los clips compartían hilo con los eventos y
    una alerta esperaba detrás de un video de ~14 s (medido: 28 s en total)."""
    monkeypatch.setenv("FISGON_API_URL", "https://api.fisgon.test")
    monkeypatch.setenv("FISGON_EDGE_TOKEN", "token-de-prueba")
    monkeypatch.setattr(upload_worker, "LATENCIA_CSV", tmp_path / "latencia_eventos.csv")
    video_en_curso = []

    def post(url, json=None, headers=None, timeout=None):
        if url.endswith("/video"):
            video_en_curso.append(True)
            time.sleep(3)  # un clip grande subiendo
            return RespuestaFalsa(200, {})
        return RespuestaFalsa(201, {"id": 900})

    monkeypatch.setattr(upload_worker.requests, "post", post)

    # Un evento anterior, ya subido, con su clip pendiente.
    anterior = buffer.save_pending(evento_confirmado, wait_for_video=True)
    buffer.mark_uploaded(anterior, remote_event_id=7)
    buffer.attach_video(anterior, "dmlkZW8=")

    worker = UploadWorker(buffer, config={}, poll_interval_seconds=POLL_LARGO)
    worker.start()
    try:
        assert _esperar(lambda: video_en_curso, segundos=1.0)  # el clip ya está subiendo

        nuevo = buffer.save_pending({**evento_confirmado, "object_class": "knife"})
        worker.nuevo_evento(nuevo, _marcas_completas())

        # La alerta sale mientras el video sigue subiendo (tarda 3 s).
        assert _esperar(lambda: buffer.get_pending() == [], segundos=1.0)
    finally:
        worker.stop()
