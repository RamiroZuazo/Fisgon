"""
upload/heartbeat.py
Manda a la nube un pulso de vida periódico con el estado de cada cámara.

Sin esto, la única señal que la web tiene de un edge son los eventos: un local
sin alertas es indistinguible de un local con el sistema caído. El pulso
permite mostrar qué cámaras están en línea, cuáles se cayeron y hace cuánto
que no dan señales.

    POST /edge/heartbeat   (header X-Edge-Token)
    {
      "uptime_seconds": 3600,
      "cameras": [
        {"camera_id": "cam_4", "name": "Tiandy", "status": "connected",
         "seconds_since_last_frame": 0.4, "seconds_since_last_inference": 1.2,
         "frames_processed": 1834},
        ...
      ]
    }

Dos decisiones de diseño:

1. **Se mandan antigüedades, no timestamps.** El edge corre en la PC del
   cliente y su reloj puede estar mal; un timestamp absoluto mostraría "última
   señal hace 3 días" en la web sin que pase nada raro. La nube recibe
   "hace 0.4 segundos" y lo convierte usando su propio reloj, que es confiable.

2. **El pulso no se reintenta ni se bufferea**, a diferencia de los eventos. Un
   pulso perdido no es información perdida: el siguiente lo reemplaza por
   completo. Reintentarlos solo llenaría la cola con estados viejos que ya no
   describen nada. Que un pulso no llegue *es* la señal de que algo anda mal.

Uso (ver main.py):
    worker = HeartbeatWorker(config, camera_manager, inference_worker)
    worker.start()
    ...
    worker.stop()
"""

import logging
import threading
import time

import requests

from upload.edge_config import _resolver_api

logger = logging.getLogger("heartbeat")

INTERVALO_POR_DEFECTO = 300  # 5 minutos
INTERVALO_MINIMO = 30        # cota, para que un valor mal cargado no inunde la API
TIMEOUT = 10


class HeartbeatWorker:
    def __init__(self, config: dict, camera_manager, inference_worker=None):
        self.camera_manager = camera_manager
        self.inference_worker = inference_worker

        configurado = (config.get("heartbeat") or {}).get("interval_seconds", INTERVALO_POR_DEFECTO)
        try:
            self.intervalo = max(int(configurado), INTERVALO_MINIMO)
        except (TypeError, ValueError):
            logger.warning(f"heartbeat.interval_seconds inválido ({configurado!r}), se usa {INTERVALO_POR_DEFECTO}s")
            self.intervalo = INTERVALO_POR_DEFECTO

        self.base_url, self.token = _resolver_api(config)
        self._arranque = time.monotonic()
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        if not self.base_url or not self.token:
            logger.warning("Sin URL o token de API — no se manda pulso de vida")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="heartbeat")
        self._thread.start()
        logger.info(f"Pulso de vida cada {self.intervalo}s")

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT + 2)

    def _run(self):
        # El primero sale enseguida: si no, la web tarda hasta un intervalo
        # entero en enterarse de que el edge volvió a levantar.
        while True:
            try:
                self._enviar()
            except Exception:
                logger.exception("Error armando o mandando el pulso de vida")
            if self._stop_event.wait(self.intervalo):
                return

    def construir_payload(self) -> dict:
        """Estado actual de todas las cámaras configuradas, corran o no."""
        sin_inferencia = {}
        procesados = {}
        if self.inference_worker is not None:
            sin_inferencia = self.inference_worker.segundos_sin_inferencia()
            procesados = self.inference_worker.get_stats()

        camaras = []
        for fila in self.camera_manager.reporte():
            cam_id = fila["camera_id"]
            camaras.append({
                **fila,
                "seconds_since_last_inference": sin_inferencia.get(cam_id),
                "frames_processed": procesados.get(cam_id, 0),
            })

        return {
            "uptime_seconds": int(time.monotonic() - self._arranque),
            "cameras": camaras,
        }

    def _enviar(self):
        payload = self.construir_payload()
        try:
            resp = requests.post(
                f"{self.base_url}/edge/heartbeat",
                json=payload,
                headers={"X-Edge-Token": self.token},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            logger.warning(f"No se pudo mandar el pulso de vida: {e}")
            return

        if resp.status_code == 404:
            logger.info("La API todavía no expone /edge/heartbeat")
            return
        if resp.status_code >= 400:
            logger.warning(f"La API rechazó el pulso de vida ({resp.status_code}): {resp.text[:200]}")
            return

        en_linea = sum(1 for c in payload["cameras"] if c["status"] == "connected")
        logger.info(f"Pulso enviado: {en_linea}/{len(payload['cameras'])} cámara(s) en línea")
