"""
upload/edge_config.py
Resuelve el cupo de cámaras que la nube define para este edge.

El cupo no es un dato del edge sino del contrato del cliente, así que vive en el
backend y viaja atado al X-Edge-Token: la API sabe qué cliente es este edge y
responde según su plan. Así, cambiar el plan no requiere entrar a la máquina del
cliente a editar un archivo.

    GET /edge/config   (header X-Edge-Token)
    → {"edge_id": "...", "max_cameras": 4, "plan": "..."}

La respuesta se cachea en .edge_config.json (raíz del proyecto, fuera de git).

Política ante fallas, pensada para un sistema de vigilancia: si la API no
responde se usa el último cupo cacheado, y si nunca hubo cache no se aplica
límite. Un problema de red no puede dejar cámaras apagadas — el riesgo de que
un cliente corra dos cámaras de más durante un corte es mucho menor que el de
un sistema de seguridad que se ciega solo. La barrera real está del lado
servidor, que rechaza los eventos de cámaras fuera de cupo; lo que hace el edge
es no pasarse y avisar.

El 404 es un camino soportado a propósito: mientras la API no exponga el
endpoint, el edge corre sin cupo y no se rompe.

Uso (ver main.py):
    from upload.edge_config import cargar_cupo
    cupo = cargar_cupo(config)          # {"max_cameras": 4|None, "origen": "..."}
"""

import json
import logging
import os
import time
from pathlib import Path

import requests

# Se reusa el saneador de URL del upload_worker en vez de repetirlo: fuerza
# https y evita un bug real y difícil de diagnosticar (ver su docstring).
from upload.upload_worker import _asegurar_https

logger = logging.getLogger("edge_config")

CACHE_PATH = Path(__file__).parent.parent / ".edge_config.json"
TIMEOUT = 10  # segundos


def _resolver_api(config: dict):
    """Devuelve (base_url, token) con el mismo criterio que upload_worker:
    .env primero, api.base_url de cameras.yaml como respaldo histórico."""
    base_url = os.environ.get("FISGON_API_URL", "").strip()
    if not base_url:
        base_url = str((config.get("api") or {}).get("base_url") or "").strip()
    token = os.environ.get("FISGON_EDGE_TOKEN", "").strip()
    return _asegurar_https(base_url.rstrip("/")), token


def _consultar_api(base_url: str, token: str):
    """Pide el cupo a la API. Devuelve el dict de la respuesta, o None si no se
    pudo obtener — el motivo queda logueado, que es lo que después permite
    diagnosticar "cambié el plan y el edge no se enteró"."""
    try:
        resp = requests.get(
            f"{base_url}/edge/config",
            headers={"X-Edge-Token": token},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        logger.warning(f"No se pudo consultar el cupo de cámaras: {e}")
        return None

    if resp.status_code == 404:
        logger.info("La API todavía no expone /edge/config — este edge corre sin cupo")
        return None
    if resp.status_code == 401:
        logger.warning("La API rechazó el token al consultar el cupo de cámaras (401)")
        return None
    if resp.status_code >= 400:
        logger.warning(f"La API respondió {resp.status_code} al consultar el cupo: {resp.text[:200]}")
        return None

    try:
        datos = resp.json()
        max_cameras = int(datos["max_cameras"])
    except (ValueError, TypeError, KeyError):
        # Si el contrato cambió, esto tiene que decir exactamente qué llegó:
        # es la diferencia entre diagnosticarlo en un minuto o en una tarde.
        logger.error(f"La API respondió sin un max_cameras válido: {resp.text[:200]}")
        return None

    if max_cameras < 1:
        logger.error(f"La API devolvió un cupo inválido ({max_cameras}) — se ignora")
        return None

    return {
        "max_cameras": max_cameras,
        "edge_id": datos.get("edge_id"),
        "plan": datos.get("plan"),
    }


def _guardar_cache(datos: dict):
    try:
        CACHE_PATH.write_text(
            json.dumps({**datos, "actualizado": time.strftime("%Y-%m-%d %H:%M:%S")}),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(f"No se pudo guardar el cupo en cache: {e}")


def leer_cupo_cacheado():
    """Último cupo conocido, o None si nunca se pudo consultar.

    La interfaz de configuración lee de acá en vez de consultar la API: el
    pipeline ya la consulta en cada arranque, y así hay un solo lugar del
    código resolviendo credenciales contra la nube.
    """
    if not CACHE_PATH.exists():
        return None
    try:
        datos = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return int(datos["max_cameras"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def cargar_cupo(config: dict) -> dict:
    """Cuántas cámaras activas puede tener este edge.

    Devuelve {"max_cameras": int | None, "origen": str}. max_cameras en None
    significa "sin cupo conocido, no se limita nada" (ver la política de fallas
    en el docstring del módulo).
    """
    base_url, token = _resolver_api(config)

    if not base_url or not token:
        logger.warning("Sin URL o token de API — no se puede consultar el cupo de cámaras")
    else:
        datos = _consultar_api(base_url, token)
        if datos is not None:
            _guardar_cache(datos)
            return {**datos, "origen": "la API"}

    cacheado = leer_cupo_cacheado()
    if cacheado is not None:
        return {"max_cameras": cacheado, "origen": "el último valor cacheado"}

    return {"max_cameras": None, "origen": "sin cupo conocido"}
