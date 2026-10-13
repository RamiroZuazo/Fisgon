"""
Pruebas de events/local_buffer.py — persistencia local de eventos (RNF de
tolerancia a cortes).

Cada prueba usa una base SQLite propia en un directorio temporal. Para simular
eventos viejos se reescribe created_at directamente, igual que se verificó a
mano durante el desarrollo (SISTEMA.md, sección 7).
"""

import pytest

from events.local_buffer import LocalBuffer


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "eventos.db"


@pytest.fixture
def buffer(db_path):
    b = LocalBuffer(db_path)
    yield b
    b.close()


def _envejecer(buffer, event_id, dias):
    with buffer._lock:
        buffer._conn.execute(
            "UPDATE events SET created_at = datetime('now', ?) WHERE id = ?",
            (f"-{dias} days", event_id),
        )
        buffer._conn.commit()


def _ids(buffer):
    with buffer._lock:
        return {fila[0] for fila in buffer._conn.execute("SELECT id FROM events")}


def test_evento_guardado_queda_pendiente_con_sus_datos(buffer, evento_confirmado):
    event_id = buffer.save_pending(evento_confirmado)

    pendientes = buffer.get_pending()
    assert [p["id"] for p in pendientes] == [event_id]
    assert pendientes[0]["bbox"] == evento_confirmado["bbox"]
    assert pendientes[0]["frames_validated"] == 3


def test_evento_subido_deja_de_estar_pendiente(buffer, evento_confirmado):
    event_id = buffer.save_pending(evento_confirmado)
    buffer.mark_uploaded(event_id, remote_event_id=42)
    assert buffer.get_pending() == []


def test_pendientes_sobreviven_al_reinicio_del_proceso(db_path, evento_confirmado):
    """Escenario de resiliencia: el proceso muere con eventos sin subir."""
    primero = LocalBuffer(db_path)
    primero.save_pending(evento_confirmado)
    primero.save_pending({**evento_confirmado, "object_class": "knife"})
    primero.close()

    reabierto = LocalBuffer(db_path)
    try:
        clases = [p["object_class"] for p in reabierto.get_pending()]
        assert clases == ["pistol", "knife"]  # se conservan en orden de llegada
    finally:
        reabierto.close()


def test_clip_interrumpido_por_reinicio_no_bloquea_la_subida(db_path, evento_confirmado):
    """Un evento esperando su clip no puede quedar trabado para siempre si el
    proceso se reinicia antes de que el clip termine."""
    primero = LocalBuffer(db_path)
    event_id = primero.save_pending(evento_confirmado, wait_for_video=True)
    primero.mark_uploaded(event_id, remote_event_id=7)
    primero.close()

    reabierto = LocalBuffer(db_path)
    try:
        assert reabierto.get_pending_videos() == []
        with reabierto._lock:
            ready, uploaded = reabierto._conn.execute(
                "SELECT video_ready, video_uploaded FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        assert (ready, uploaded) == (1, 1)
    finally:
        reabierto.close()


def test_video_adjunto_queda_pendiente_hasta_subirse(buffer, evento_confirmado):
    event_id = buffer.save_pending(evento_confirmado, wait_for_video=True)
    buffer.mark_uploaded(event_id, remote_event_id=7)
    assert buffer.get_pending_videos() == []  # el clip todavía no está listo

    buffer.attach_video(event_id, "dmlkZW8=")
    videos = buffer.get_pending_videos()
    assert [(v["id"], v["remote_event_id"]) for v in videos] == [(event_id, 7)]

    buffer.mark_video_uploaded(event_id)
    assert buffer.get_pending_videos() == []


# --- Regresión: limpieza de eventos antiguos (SISTEMA.md, sección 7.2) -------

def test_limpieza_borra_viejo_y_subido(buffer, evento_confirmado):
    event_id = buffer.save_pending(evento_confirmado)
    buffer.mark_uploaded(event_id, remote_event_id=1)
    _envejecer(buffer, event_id, dias=10)

    assert buffer.limpiar_antiguos(dias=7) == 1
    assert event_id not in _ids(buffer)


def test_limpieza_conserva_reciente_y_subido(buffer, evento_confirmado):
    event_id = buffer.save_pending(evento_confirmado)
    buffer.mark_uploaded(event_id, remote_event_id=1)
    _envejecer(buffer, event_id, dias=2)

    assert buffer.limpiar_antiguos(dias=7) == 0
    assert event_id in _ids(buffer)


def test_limpieza_nunca_borra_un_pendiente_aunque_sea_viejo(buffer, evento_confirmado):
    event_id = buffer.save_pending(evento_confirmado)
    _envejecer(buffer, event_id, dias=30)

    assert buffer.limpiar_antiguos(dias=7) == 0
    assert [p["id"] for p in buffer.get_pending()] == [event_id]


def test_limpieza_conserva_evento_con_video_sin_subir(buffer, evento_confirmado):
    event_id = buffer.save_pending(evento_confirmado, wait_for_video=True)
    buffer.attach_video(event_id, "dmlkZW8=")
    buffer.mark_uploaded(event_id, remote_event_id=1)
    _envejecer(buffer, event_id, dias=30)

    assert buffer.limpiar_antiguos(dias=7) == 0
    assert event_id in _ids(buffer)
