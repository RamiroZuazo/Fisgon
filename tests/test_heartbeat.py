"""
Pruebas de upload/heartbeat.py — pulso de vida con el estado de las cámaras.
"""

import pytest

from upload.heartbeat import INTERVALO_MINIMO, INTERVALO_POR_DEFECTO, HeartbeatWorker


class CamarasFalsas:
    def reporte(self):
        return [
            {"camera_id": "cam_1", "name": "Entrada", "status": "connected", "seconds_since_last_frame": 0.4},
            {"camera_id": "cam_2", "name": "Depósito", "status": "disconnected", "seconds_since_last_frame": None},
        ]


class InferenciaFalsa:
    def segundos_sin_inferencia(self):
        return {"cam_1": 1.2}

    def get_stats(self):
        return {"cam_1": 1834}


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setenv("FISGON_API_URL", "https://api.fisgon.test")
    monkeypatch.setenv("FISGON_EDGE_TOKEN", "token-de-prueba")


def test_payload_reporta_todas_las_camaras_con_antiguedades():
    payload = HeartbeatWorker({}, CamarasFalsas(), InferenciaFalsa()).construir_payload()

    assert payload["uptime_seconds"] >= 0
    cam_1, cam_2 = payload["cameras"]
    assert cam_1["seconds_since_last_inference"] == 1.2 and cam_1["frames_processed"] == 1834
    # Una cámara caída también se reporta: su ausencia es justamente la señal.
    assert cam_2["status"] == "disconnected"
    assert cam_2["seconds_since_last_inference"] is None and cam_2["frames_processed"] == 0
    # Se mandan antigüedades, no timestamps: el reloj del edge no es confiable.
    assert not any("timestamp" in c for c in payload["cameras"])


@pytest.mark.parametrize("configurado, esperado", [
    (5, INTERVALO_MINIMO),
    (600, 600),
    ("abc", INTERVALO_POR_DEFECTO),
], ids=["acotado-al-minimo", "valor-valido", "valor-invalido"])
def test_intervalo_configurado_se_acota(configurado, esperado):
    worker = HeartbeatWorker({"heartbeat": {"interval_seconds": configurado}}, CamarasFalsas())
    assert worker.intervalo == esperado
