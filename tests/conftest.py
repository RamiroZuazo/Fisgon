"""
tests/conftest.py
Configuración compartida de la suite de pruebas del edge.

Los módulos se importan como en main.py (`from events.event_validator import
...`), así que la raíz del proyecto tiene que estar en sys.path.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def frame():
    """Frame negro chico: alcanza para que event_validator dibuje y codifique
    el snapshot sin depender de una cámara."""
    return np.zeros((48, 64, 3), dtype="uint8")


@pytest.fixture
def evento_confirmado():
    """Evento con la forma que produce event_validator y guarda local_buffer."""
    return {
        "camera_id": "cam_test",
        "object_class": "pistol",
        "confidence": 0.91,
        "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
        "frames_validated": 3,
        "snapshot_base64": "c25hcHNob3Q=",
        "timestamp": "2026-10-13T12:00:00+00:00",
    }


class RespuestaFalsa:
    """Sustituto mínimo de requests.Response para no salir a la red."""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("sin cuerpo JSON")
        return self._json
