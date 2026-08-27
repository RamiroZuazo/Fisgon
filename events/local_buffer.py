"""
events/local_buffer.py
Persistencia local de eventos confirmados pendientes de subir a la nube.

Usa SQLite (un solo archivo, sin servidor) para que los eventos sobrevivan a
reinicios del proceso o cortes de luz: un evento solo se marca como subido
después de que upload_worker confirma el POST a la API.

Uso típico (ver main.py):
    from events.local_buffer import LocalBuffer
    buffer = LocalBuffer("eventos.db")
    buffer.save_pending(payload)          # desde event_validator
    pendientes = buffer.get_pending()     # desde upload_worker
    buffer.mark_uploaded(pendientes[0]["id"])
"""

import json
import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger("local_buffer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    object_class TEXT NOT NULL,
    confidence REAL NOT NULL,
    bbox TEXT NOT NULL,
    frames_validated INTEGER NOT NULL DEFAULT 1,
    snapshot_base64 TEXT,
    video_base64 TEXT,
    timestamp TEXT NOT NULL,
    uploaded INTEGER NOT NULL DEFAULT 0,
    remote_event_id INTEGER,
    video_ready INTEGER NOT NULL DEFAULT 1,
    video_uploaded INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class LocalBuffer:
    def __init__(self, db_path: str = "eventos.db"):
        self.db_path = str(db_path)
        if Path(self.db_path).parent != Path("."):
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        # check_same_thread=False: event_validator (thread de inferencia) escribe,
        # upload_worker (thread propio) lee/actualiza; el Lock serializa el acceso.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(SCHEMA)
        self._migrar_columnas_video()
        self._recuperar_clips_interrumpidos()
        self._conn.commit()

    def _migrar_columnas_video(self):
        """Actualiza eventos.db existentes sin perder eventos pendientes."""
        columnas = {fila[1] for fila in self._conn.execute("PRAGMA table_info(events)")}
        if "video_base64" not in columnas:
            self._conn.execute("ALTER TABLE events ADD COLUMN video_base64 TEXT")
        if "remote_event_id" not in columnas:
            self._conn.execute("ALTER TABLE events ADD COLUMN remote_event_id INTEGER")
        if "video_ready" not in columnas:
            self._conn.execute("ALTER TABLE events ADD COLUMN video_ready INTEGER NOT NULL DEFAULT 1")
        if "video_uploaded" not in columnas:
            self._conn.execute("ALTER TABLE events ADD COLUMN video_uploaded INTEGER NOT NULL DEFAULT 1")

    def _recuperar_clips_interrumpidos(self):
        """Un clip en generación no sobrevive al reinicio; libera esos eventos."""
        self._conn.execute(
            "UPDATE events SET video_ready = 1, video_uploaded = 1 "
            "WHERE video_ready = 0"
        )

    def save_pending(self, event: dict, wait_for_video: bool = False) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO events (camera_id, object_class, confidence, bbox, "
                "frames_validated, snapshot_base64, video_base64, timestamp, video_ready, video_uploaded) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event["camera_id"],
                    event["object_class"],
                    event["confidence"],
                    json.dumps(event["bbox"]),
                    event.get("frames_validated", 1),
                    event.get("snapshot_base64"),
                    event.get("video_base64"),
                    event["timestamp"],
                    0 if wait_for_video else 1,
                    0 if wait_for_video else 1,
                ),
            )
            self._conn.commit()
            event_id = cursor.lastrowid
        logger.info(f"[{event['camera_id']}] Evento guardado localmente: {event['object_class']}")
        return event_id

    def attach_video(self, event_id: int, video_base64: str | None):
        """Adjunta el clip y habilita la subida, incluso si el video falló."""
        with self._lock:
            self._conn.execute(
                "UPDATE events SET video_base64 = ?, video_ready = 1, video_uploaded = ? WHERE id = ?",
                (video_base64, 0 if video_base64 else 1, event_id),
            )
            self._conn.commit()
        if video_base64:
            logger.info("Video adjuntado al evento local #%s", event_id)
        else:
            logger.warning("Evento local #%s continuará sin video", event_id)

    def get_pending(self) -> list:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, camera_id, object_class, confidence, bbox, frames_validated, "
                "snapshot_base64, video_base64, timestamp FROM events "
                "WHERE uploaded = 0 ORDER BY id ASC"
            )
            filas = cursor.fetchall()

        return [
            {
                "id": fila[0],
                "camera_id": fila[1],
                "object_class": fila[2],
                "confidence": fila[3],
                "bbox": json.loads(fila[4]),
                "frames_validated": fila[5],
                "snapshot_base64": fila[6],
                "video_base64": fila[7],
                "timestamp": fila[8],
            }
            for fila in filas
        ]

    def mark_uploaded(self, event_id: int, remote_event_id: int | None = None):
        with self._lock:
            self._conn.execute(
                "UPDATE events SET uploaded = 1, remote_event_id = COALESCE(?, remote_event_id) WHERE id = ?",
                (remote_event_id, event_id),
            )
            self._conn.commit()

    def get_pending_videos(self) -> list:
        with self._lock:
            filas = self._conn.execute(
                "SELECT id, remote_event_id, camera_id, video_base64 FROM events "
                "WHERE uploaded = 1 AND remote_event_id IS NOT NULL "
                "AND video_ready = 1 AND video_uploaded = 0 AND video_base64 IS NOT NULL "
                "ORDER BY id ASC"
            ).fetchall()
        return [
            {"id": fila[0], "remote_event_id": fila[1], "camera_id": fila[2], "video_base64": fila[3]}
            for fila in filas
        ]

    def mark_video_uploaded(self, event_id: int):
        with self._lock:
            self._conn.execute("UPDATE events SET video_uploaded = 1 WHERE id = ?", (event_id,))
            self._conn.commit()

    def limpiar_antiguos(self, dias: int = 7) -> int:
        """Borra eventos ya subidos (uploaded=1) con más de `dias` de
        antigüedad y compacta el archivo. Sin esto, eventos.db crece sin
        límite corriendo 24/7 — los eventos ya están en la nube (fuente de
        verdad), no hace falta guardarlos localmente para siempre. Pensado
        para llamarse periódicamente (ver upload_worker), no en cada ciclo.
        Devuelve la cantidad de filas borradas."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM events WHERE uploaded = 1 AND video_uploaded = 1 "
                "AND created_at < datetime('now', ?)",
                (f"-{dias} days",),
            )
            borrados = cursor.rowcount
            self._conn.commit()
            if borrados > 0:
                self._conn.execute("VACUUM")
        return borrados

    def close(self):
        with self._lock:
            self._conn.close()


if __name__ == "__main__":
    # Prueba aislada: guarda un evento falso, lo lee y lo marca como subido
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    buffer = LocalBuffer("test_eventos.db")
    buffer.save_pending({
        "camera_id": "cam_test",
        "object_class": "knife",
        "confidence": 0.87,
        "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
        "frames_validated": 3,
        "snapshot_base64": None,
        "timestamp": "2026-01-01T00:00:00+00:00",
    })

    pendientes = buffer.get_pending()
    print(f"Pendientes: {pendientes}")
    buffer.mark_uploaded(pendientes[0]["id"])
    print(f"Pendientes tras marcar subido: {buffer.get_pending()}")
    buffer.close()
