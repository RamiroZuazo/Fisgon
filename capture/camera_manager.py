"""
capture/camera_manager.py
Orquesta múltiples CameraSource a partir de cameras.yaml.

Lee la lista de cámaras del YAML, crea un CameraSource por cada cámara con
`active: true`, y las hace compartir la misma Queue de frames que después
consume inference_worker.py.

Si el plan del cliente define un cupo (`max_cameras`, ver upload/edge_config.py),
arranca solo esa cantidad y deja las demás sin iniciar, avisando cuáles.

Uso típico (ver main.py para el pipeline completo):

    from queue import Queue
    from capture.camera_manager import CameraManager

    cola = Queue(maxsize=200)
    manager = CameraManager(config, frame_queue=cola)
    manager.start_all()
    ...
    estado = manager.get_status()  # {"cam_1": "connected", ...}
    ...
    manager.stop_all()
"""

import logging
import sys
from pathlib import Path

# Permite ejecutar este archivo directamente (python capture/camera_manager.py)
# además de importarlo como capture.camera_manager desde main.py
sys.path.insert(0, str(Path(__file__).parent.parent))

from capture.camera_source import CameraSource, INFERENCE_FPS

logger = logging.getLogger("camera_manager")


class CameraManager:
    def __init__(self, config: dict, frame_queue, on_video_frame=None, max_cameras=None):
        """
        config: dict ya cargado de cameras.yaml (secciones "cameras" e "inference").
        frame_queue: Queue compartida donde CameraSource encola frames.
        max_cameras: cupo del plan, o None para no limitar.
        """
        self.frame_queue = frame_queue
        inference_fps = config.get("inference", {}).get("inference_fps", INFERENCE_FPS)
        motion_config = config.get("motion_detection", {})

        activas = []
        for cam_cfg in config.get("cameras", []):
            if not cam_cfg.get("active", False):
                logger.info(f"[{cam_cfg.get('id')}] Inactiva en config, se omite")
                continue
            activas.append(cam_cfg)

        # El cupo se mide sobre las activas en config, no sobre las que están
        # conectadas: si se contaran las conectadas, una cámara que se reconecta
        # después de un corte quedaría adentro o afuera según quién gane la
        # carrera. Se recortan las últimas del archivo — determinístico, el
        # mismo cameras.yaml arranca siempre las mismas cámaras.
        self.fuera_de_cupo = []
        if max_cameras is not None and len(activas) > max_cameras:
            self.fuera_de_cupo = activas[max_cameras:]
            ids = ", ".join(str(cam.get("id")) for cam in self.fuera_de_cupo)
            logger.warning(
                f"El plan permite {max_cameras} cámara(s) activa(s) y hay {len(activas)} "
                f"en la configuración — no se arrancan: {ids}"
            )
            activas = activas[:max_cameras]

        # Se guarda la config de todas para poder reportar también las que no
        # están corriendo: la web tiene que poder mostrar "configurada pero
        # desactivada" o "fuera del cupo del plan", no solo las que andan.
        self._config_camaras = list(config.get("cameras", []))

        self._sources = {}
        for cam_cfg in activas:
            self._sources[cam_cfg["id"]] = CameraSource(
                camera_id=cam_cfg["id"],
                rtsp_url=cam_cfg["rtsp_url"],
                frame_queue=frame_queue,
                inference_fps=inference_fps,
                motion_config=motion_config,
                on_video_frame=on_video_frame,
            )

        if not self._sources:
            logger.warning("No hay cámaras activas en la configuración")

    def start_all(self):
        for camera_id, source in self._sources.items():
            logger.info(f"[{camera_id}] Iniciando...")
            source.start()

    def stop_all(self):
        for camera_id, source in self._sources.items():
            logger.info(f"[{camera_id}] Deteniendo...")
            source.stop()

    def get_status(self) -> dict:
        """Devuelve {camera_id: estado} con estado en
        connecting/connected/reconnecting/failed/stopped."""
        return {camera_id: source.status for camera_id, source in self._sources.items()}

    def reporte(self) -> list:
        """Estado de todas las cámaras configuradas, corran o no.

        Pensado para el pulso de vida que se manda a la nube (ver
        upload/heartbeat.py), así la web puede distinguir una cámara caída de
        una que el operador desactivó o que el plan no permite.
        """
        fuera_de_cupo = {str(cam.get("id")) for cam in self.fuera_de_cupo}
        filas = []
        for cam_cfg in self._config_camaras:
            cam_id = str(cam_cfg.get("id"))
            source = self._sources.get(cam_id)
            if source is not None:
                estado = source.status
                sin_frame = source.segundos_sin_frame()
            elif cam_id in fuera_de_cupo:
                estado, sin_frame = "out_of_quota", None
            else:
                estado, sin_frame = "disabled", None
            filas.append({
                "camera_id": cam_id,
                "name": cam_cfg.get("name"),
                "status": estado,
                "seconds_since_last_frame": sin_frame,
            })
        return filas


if __name__ == "__main__":
    # Prueba aislada: arranca las cámaras activas de cameras.yaml y muestra
    # su estado cada 5 segundos. Ctrl+C para salir.
    import time
    import sys
    from pathlib import Path
    from queue import Queue

    import yaml

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent / "cameras.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    cola = Queue(maxsize=200)
    manager = CameraManager(config, frame_queue=cola)
    manager.start_all()

    try:
        while True:
            time.sleep(5)
            print(f"Estado: {manager.get_status()} | frames en cola: {cola.qsize()}")
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop_all()
