# Contexto del proyecto — Módulo Edge de Captura e Inferencia (PFI)

## Qué es este proyecto

Proyecto Final Integrador (PFI) universitario: sistema de detección de armas
(pistolas y cuchillos) en cámaras CCTV usando un modelo YOLOv8, para
presentación de tesis en diciembre.

Este documento describe específicamente el **módulo edge**: el componente que
corre en hardware local (no en la nube), captura video RTSP de hasta 4
cámaras/canales de un DVR, corre inferencia, y sube eventos confirmados a una
API en la nube. Es una pieza dentro de un sistema más grande (API + dashboard),
pero este contexto se enfoca solo en el edge.

## Stack y restricciones de hardware — IMPORTANTE, no cambiar sin confirmar

- **SO: Windows** (decisión ya tomada, no migrar a Linux sin que el usuario lo
  pida explícitamente — se evaluó Linux/ROCm y se descartó por riesgo de
  incompatibilidad de la GPU con versiones recientes de ROCm)
- **GPU: AMD RX 580 8GB** (arquitectura Polaris/gfx803) — NO tiene CUDA. La
  inferencia corre vía **ONNX Runtime + DirectML** (`onnxruntime-directml`),
  no PyTorch directo con GPU.
- El modelo YOLOv8 se exporta de `.pt` a `.onnx` antes de usarse en producción.
- Rendimiento medido: **1 cámara sola corre a 17-18 fps** de inferencia en
  esta GPU (con ventana de visualización activa; sin visualización debería
  rendir igual o mejor).
- **Meta de escala: máximo 4 cámaras simultáneas** (suficiente para la
  presentación de tesis, no hace falta optimizar para más).

## Decisión de diseño clave: FPS de inferencia vs FPS de captura

No se corre inferencia en cada frame del stream RTSP (que puede venir a
25-30fps). Se captura a FPS nativo pero se **submuestrea a un FPS fijo de 5
por cámara** antes de encolar para inferencia. Con 4 cámaras a 5fps cada una
= 20 fps de inferencia total, que la RX 580 maneja con margen (ya probado que
aguanta 17-18fps con una sola cámara a resolución completa).

Este valor (`INFERENCE_FPS = 5`) es una constante simple por ahora, no
configurable por cámara — el usuario prefirió empezar así, se puede hacer
configurable después si hace falta.

## Interfaz de configuración (YA ESCRITA — no reescribir desde cero)

`config_ui/app.py` + `config_ui/templates/index.html` + `config_ui/static/style.css`

Servidor FastAPI local (`localhost:8000`, corre aparte de `main.py`, no forma
parte del pipeline) para editar `cameras.yaml` desde el navegador en vez de a
mano. Permite agregar/editar/eliminar cámaras, ajustar `confidence`, `iou`,
`inference_fps`, `min_consecutive_detections`, `window_seconds` y
`api.base_url`, cargar la URL y el token de la API (que se guardan en `.env`,
no en `cameras.yaml`), y tiene un botón "Probar conexión" por cámara que abre el
RTSP brevemente con `cv2.VideoCapture` (la misma librería que usa
`camera_source.py`) para confirmar que conecta antes de guardar.

Los cambios se aplican reiniciando `main.py`: no hay sincronización en
caliente entre esta interfaz y el pipeline principal, por diseño (simplicidad
para el alcance de la tesis). Ese reinicio está automatizado — al guardar,
`config_ui` deja el archivo `.reiniciar_pipeline` en la raíz y `run.py`, que
supervisa ambos procesos, lo ve y reinicia solo `main.py` (la interfaz sigue
arriba y la sesión no se corta). Si `config_ui` corre suelto, sin `run.py`,
la señal no tiene efecto y el reinicio queda a mano como antes.

## Ya existe (código de referencia)

### `fisgon_amd.py` (script de referencia del usuario, adjunto en el repo)
Script standalone que:
- Carga YOLOv8 exportado a ONNX
- Activa DirectML parcheando `onnxruntime.InferenceSession` para forzar
  `DmlExecutionProvider`
- Corre inferencia en loop sobre una sola webcam (`cv2.VideoCapture(int)`)
- Tiene un limitador de FPS de captura basado en `cv2.getTickCount()`

Este script es la base para `inference/model_loader.py` (reusar
`activar_directml()`) y confirma que `cv2.VideoCapture` también acepta
directamente una URL RTSP en vez de un índice de cámara — no hace falta
cambiar nada de OpenCV para pasar de webcam a RTSP.

### `capture/camera_source.py` (YA ESCRITO — no reescribir desde cero)
Clase `CameraSource` que:
- Corre en su propio thread (uno por cámara)
- Se conecta a una URL RTSP con `cv2.VideoCapture`
- Reconecta automáticamente con backoff (2s, 5s, 10s, 30s) si el stream se
  cae o no conecta
- Submuestrea a `INFERENCE_FPS` (5 por defecto) antes de encolar frames
- Encola `{camera_id, frame, timestamp}` en una `Queue` compartida pasada por
  constructor
- Si la cola está llena, descarta el frame nuevo (no bloquea) — para que una
  cámara lenta no trabe a las demás
- Oculta credenciales (`user:pass@`) en los logs

Ya está validado sintácticamente. Falta testear contra el DVR real del
usuario (Hikvision, RTSP en puerto 554, credenciales aún por confirmar con el
usuario — está resolviendo un bloqueo temporal de login).

## Falta construir (en este orden)

### 1. `capture/camera_manager.py`
- Lee la lista de cámaras desde `cameras.yaml` (ver formato abajo)
- Crea una `CameraSource` por cada cámara con `active: true`
- Todas comparten la misma `Queue`
- Expone `start_all()`, `stop_all()`, y un método para consultar el estado
  (`connected`/`reconnecting`/`failed`) de cada cámara — útil para logging y
  para el dashboard más adelante

### 2. `inference/model_loader.py`
- Extraer y reusar la lógica de `activar_directml()` de `fisgon_amd.py`
- Cargar el modelo ONNX una sola vez (ruta desde `cameras.yaml`)

### 3. `inference/inference_worker.py`
- UN SOLO thread/worker consumiendo la cola compartida (evita problemas de
  concurrencia sobre la misma sesión ONNX — nunca dos threads llaman
  `predict()` a la vez)
- Por cada frame: corre `modelo.predict()`, si hay detección con
  `confidence >= umbral` (de `cameras.yaml`), pasa el resultado a
  `event_validator`

### 4. `events/event_validator.py`
- Mantiene un historial corto de detecciones por `camera_id` (ventana de
  tiempo configurable, ej. últimos 5 segundos)
- Confirma un "evento real" solo si hay >= N detecciones consecutivas/dentro
  de la ventana (evita falsos positivos de un solo frame) — N configurable en
  `cameras.yaml` (`min_consecutive_detections`)
- Al confirmar, arma el payload: `camera_id`, `object_class`, `confidence`,
  `bbox`, `snapshot_base64` (el frame de la detección), `timestamp`
- Pasa el payload a `local_buffer`

### 5. `events/local_buffer.py`
- Persistencia local (SQLite simple) de eventos confirmados pendientes de
  subir
- Métodos: `save_pending(event)`, `get_pending()`, `mark_uploaded(event_id)`
- Debe sobrevivir a reinicios del proceso (si se corta la luz, no se pierden
  eventos)

### 6. `upload/upload_worker.py`
- Thread separado, en loop: cada N segundos revisa `local_buffer` por
  eventos pendientes
- Hace `POST` a la API en la nube (FastAPI, ya existe — ver `events` table
  schema abajo)
- Si falla (sin conexión, error 5xx), deja el evento como pendiente y
  reintenta en el próximo ciclo — nunca lo descarta
- Si tiene éxito, marca `mark_uploaded`

### 7. `main.py`
- Lee `cameras.yaml`
- Instancia y arranca `camera_manager`, `inference_worker`, `upload_worker`
- Logging a consola + archivo (`logs/edge.log`)
- Maneja `Ctrl+C` / `SIGINT` para cerrar todos los threads ordenadamente
  (`stop_all()` en cascada)

## Formato de `cameras.yaml` (config declarativa, versionable)

```yaml
inference:
  model_path: "models/best.onnx"
  confidence: 0.5
  iou: 0.45
  inference_fps: 5

event_validation:
  min_consecutive_detections: 3
  window_seconds: 5

api:
  base_url: "https://tu-api.railway.app"
  # api_key vía variable de entorno, no hardcodeada acá

cameras:
  - id: "cam_1"
    name: "Entrada principal"
    rtsp_url: "rtsp://usuario:password@192.168.1.22:554/Streaming/Channels/101"
    active: true
```

Las credenciales RTSP quedan en este archivo por ahora (alcance acotado de
tesis, entorno controlado). Mencionar como mejora futura: mover a variables
de entorno o secret manager. La `api_key` de la nube SÍ debe ir por variable
de entorno desde el principio, no en el YAML.

## Contexto de la API en la nube (ya existe, no tocar en este módulo)

- FastAPI + SQLAlchemy, compatible con PostgreSQL vía `DATABASE_URL`
- Tabla `events`: `object_class` (enum: "pistol" | "knife"), `confidence`,
  `bbox`, `snapshot_base64`, `review_status`
- Auth opcional vía API key
- Hosting interino: Supabase + Railway (se migra a AWS/Azure más cerca de la
  defensa de diciembre)
- El edge sube eventos vía `POST` a esta API — el módulo edge NO implementa
  la API, solo la consume.

## Convenciones del proyecto

- Comentarios y docstrings en español (así está `camera_source.py` y
  `fisgon_amd.py`)
- Nombres de variables/funciones en español donde ya es el patrón existente
  (ej. `activar_directml`, `resolver_modelo`), en inglés donde es más natural
  para términos técnicos estándar (`frame_queue`, `event_validator`) —
  seguir el estilo mixto que ya tiene `fisgon_amd.py`, no forzar todo a un
  idioma
- Sin frameworks pesados nuevos: usar `threading` estándar de Python, no
  `asyncio` ni colas externas (Redis, RabbitMQ) — la escala (4 cámaras) no
  lo justifica y complica la defensa de tesis sin necesidad
- Repositorio git: `PFI-Fisgon-Script-Etiquetas`

## Qué NO hacer

- No migrar a Linux/ROCm sin pedido explícito del usuario (ya se evaluó y
  descartó, ver arriba)
- No usar CUDA/PyTorch-GPU directo — la GPU es AMD, todo pasa por ONNX
  Runtime + DirectML
- No sobre-ingenierizar la concurrencia (microservicios, colas distribuidas)
  — es un solo proceso Python con threads, para 4 cámaras alcanza de sobra
- No implementar autenticación/gestión de usuarios en el edge — eso vive en
  la API/dashboard, fuera de este módulo

## Instrucciones para Claude Code

Encargate de construir **todo** lo que falta (sección "Falta construir" de
arriba), de punta a punta, en el orden listado ahí (1 a 7). No te detengas a
pedir confirmación entre módulo y módulo salvo que encuentres una ambigüedad
real que no esté resuelta en este documento.

Para cada módulo:
- Seguí el estilo ya establecido en `capture/camera_source.py` (logging con
  `logging.getLogger`, manejo de `threading.Event` para stop ordenado,
  docstring de módulo al principio explicando qué hace y cómo se usa,
  comentarios en español)
- Reusá código existente en vez de reescribirlo: `activar_directml()` y la
  carga de modelo ONNX salen de `fisgon_amd.py`; el formato de item de cola
  (`{camera_id, frame, timestamp}`) ya está definido en `camera_source.py`,
  no lo cambies
- Al terminar cada módulo, dejalo importable y con un bloque
  `if __name__ == "__main__":` mínimo para poder testearlo de forma aislada
  cuando tenga sentido (por ejemplo, `inference_worker.py` podría testearse
  con un frame de prueba sin necesitar todo el pipeline corriendo)
- Agregá un `requirements.txt` en la raíz de `edge/` con todas las
  dependencias usadas (`opencv-python`, `onnxruntime-directml`,
  `ultralytics`, `pyyaml`, `fastapi`, `uvicorn`, `jinja2`, `requests` o
  `httpx` para el upload_worker, etc.)

Al final, `main.py` tiene que poder correrse con:
```
python main.py --config cameras.yaml
```
y levantar todo el pipeline (cámaras → inferencia → validación de eventos →
buffer local → subida a la nube) de forma estable, con logs claros de qué
está pasando en cada etapa.

Si en algún punto hace falta tomar una decisión de diseño que no está cubierta
en este documento (por ejemplo, formato exacto de un endpoint de la API que
no se detalla acá), lo mejor es dejar el código funcional con un supuesto
razonable documentado en un comentario, en vez de bloquearse — el usuario
puede ajustar el detalle después.
