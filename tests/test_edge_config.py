"""
Pruebas de upload/edge_config.py — cupo de cámaras definido por la nube.

La política que se verifica es la del docstring del módulo: ante una falla de
la API se usa el último cupo cacheado, y si nunca hubo cache no se limita
nada. Un problema de red no puede dejar cámaras apagadas.
"""

import json

import pytest
import requests

from conftest import RespuestaFalsa
from upload import edge_config


@pytest.fixture(autouse=True)
def entorno(tmp_path, monkeypatch):
    monkeypatch.setattr(edge_config, "CACHE_PATH", tmp_path / ".edge_config.json")
    monkeypatch.setenv("FISGON_API_URL", "https://api.fisgon.test")
    monkeypatch.setenv("FISGON_EDGE_TOKEN", "token-de-prueba")


def _responder(monkeypatch, respuesta):
    def get(url, headers=None, timeout=None):
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta
    monkeypatch.setattr(edge_config.requests, "get", get)


def _cachear(max_cameras):
    edge_config.CACHE_PATH.write_text(json.dumps({"max_cameras": max_cameras}), encoding="utf-8")


def test_cupo_de_la_api_se_usa_y_se_cachea(monkeypatch):
    _responder(monkeypatch, RespuestaFalsa(200, {"edge_id": "e1", "max_cameras": 4, "plan": "pyme"}))

    cupo = edge_config.cargar_cupo({})

    assert cupo["max_cameras"] == 4 and cupo["origen"] == "la API"
    assert edge_config.leer_cupo_cacheado() == 4


@pytest.mark.parametrize("respuesta", [
    requests.ConnectionError("sin red"),
    RespuestaFalsa(500, text="error"),
    RespuestaFalsa(401, text="token inválido"),
], ids=["sin-red", "error-500", "token-401"])
def test_si_la_api_falla_se_usa_el_ultimo_cupo_cacheado(monkeypatch, respuesta):
    _cachear(2)
    _responder(monkeypatch, respuesta)

    cupo = edge_config.cargar_cupo({})

    assert cupo == {"max_cameras": 2, "origen": "el último valor cacheado"}


def test_sin_api_ni_cache_no_se_limitan_las_camaras(monkeypatch):
    _responder(monkeypatch, requests.ConnectionError("sin red"))
    assert edge_config.cargar_cupo({})["max_cameras"] is None


def test_api_sin_el_endpoint_404_no_rompe_el_edge(monkeypatch):
    _responder(monkeypatch, RespuestaFalsa(404))
    assert edge_config.cargar_cupo({})["max_cameras"] is None


@pytest.mark.parametrize("cuerpo", [{"max_cameras": 0}, {"max_cameras": "muchas"}, {"plan": "pyme"}],
                         ids=["cero", "no-numerico", "sin-campo"])
def test_cupo_invalido_de_la_api_se_ignora(monkeypatch, cuerpo):
    _cachear(3)
    _responder(monkeypatch, RespuestaFalsa(200, cuerpo))

    assert edge_config.cargar_cupo({})["max_cameras"] == 3


def test_url_http_se_consulta_por_https(monkeypatch):
    monkeypatch.setenv("FISGON_API_URL", "http://api.fisgon.app/")
    base_url, _ = edge_config._resolver_api({})
    assert base_url == "https://api.fisgon.app"
