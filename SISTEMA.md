# Fisgon — Documentación completa del módulo edge

Este documento explica **todo** el sistema tal como quedó: qué hace cada
pieza, por qué se construyó así, qué problemas reales aparecieron durante
el desarrollo y cómo se resolvieron. Está pensado para que alguien que no
estuvo presente durante el desarrollo (vos en unos meses, un compañero, un
jurado de tesis) pueda entender el sistema completo sin tener que leer el
código de punta a punta primero.

Para instrucciones de instalación paso a paso, ver `INSTALACION.md`. Para
el contexto original del encargo, ver `CONTEXT.md`.

---

## 1. Qué es esto

Módulo edge de un sistema de detección de armas (pistolas y cuchillos) en
cámaras de seguridad, para una tesis (PFI) con defensa en diciembre. Corre
en una PC local (no en la nube), captura video de hasta 4 cámaras
RTSP/DVR, corre un modelo YOLOv8 para detectar armas, y sube los eventos
confirmados a una API en la nube (que sí implementa otro equipo — este
módulo solo la consume).

**Hardware fijo de esta PC**: GPU AMD RX 580 (arquitectura Polaris, sin
CUDA). La inferencia corre por **ONNX Runtime + DirectML**, no PyTorch con
GPU directo. Esto no es negociable para el proyecto — ver `CONTEXT.md` para
el porqué (se evaluó Linux/ROCm y se descartó).

---

## 2. Arquitectura: el flujo completo

```
                    ┌─────────────────────────────────────────┐
                    │              run.py (opcional)           │
                    │  levanta main.py + config_ui juntos,     │
                    │  reinicia cualquiera de los dos si crashea│
                    └───────────────┬───────────────┬──────────┘
                                    │               │
                    ┌───────────────▼──┐    ┌───────▼────────────┐
                    │     main.py       │    │  config_ui/app.py  │
                    │  (pipeline)       │    │  (panel web,       │
                    │                   │    │   localhost:8000)  │
                    └───────────────────┘    └─────────────────────┘

Dentro de main.py:

  cámara 1 ──┐
  cámara 2 ──┼─► capture/camera_source.py (1 thread c/u)
  cámara 3 ──┤     - reconecta solo si se cae el stream
  cámara 4 ──┘     - filtro de movimiento (no manda frames si no cambia nada)
                    - submuestrea a inference_fps (5 por defecto)
                          │
                          ▼
                  Queue compartida (frame_queue)
                          │
                          ▼
          inference/inference_worker.py (1 SOLO thread)
          - junta lo que ya esté en la cola en un lote (batching)
          - corre el modelo YOLOv8 una vez por lote
                          │
                          ▼
          events/event_validator.py
          - exige N detecciones en una ventana de tiempo antes
            de confirmar (evita falsos positivos de 1 frame)
                          │
                          ▼
          events/local_buffer.py (SQLite: eventos.db)
          - persiste el evento localmente ANTES de intentar subirlo
                          │
                          ▼
          upload/upload_worker.py (1 thread propio)
          - POST a la API en la nube con reintentos
          - limpia eventos.db periódicamente
```

**Por qué threading simple y no `asyncio` ni colas externas (Redis, etc.)**:
la escala del proyecto (4 cámaras) no lo justifica, y hubiera complicado la
defensa de tesis sin necesidad — decisión ya tomada en `CONTEXT.md`.

---

## 3. Cada módulo, explicado

### `capture/camera_source.py`
Una instancia por cámara, cada una en su propio thread. Se conecta con
`cv2.VideoCapture`, reconecta con backoff (2s, 5s, 10s, 30s) si el stream
se cae, y **submuestrea**: aunque la cámara entregue frames a su ritmo
nativo (una webcam USB típica: ~28-30fps), solo se decide encolar 1 de
cada varios, al ritmo configurado en `inference_fps`.

**Filtro de movimiento (agregado durante el desarrollo)**: compara cada
frame candidato contra el anterior (diferencia en escala de grises,
reducida a 160x90 para que sea barato). Si no cambió nada significativo,
no lo manda a inferencia. Tiene dos modos:
- **Activo**: mientras hay movimiento reciente, encola a `inference_fps`
  completo (ej. 5fps).
- **Reposo**: si no hay movimiento hace más de `cooldown_seconds`, baja a
  `floor_fps` (1fps por defecto) — **no corta del todo**, para no perder
  un arma que alguien sostiene quieta en cámara. Esto fue una decisión
  explícita: un filtro de movimiento puro sería un punto ciego real para
  un sistema de detección de armas.

Todo esto se configura en `cameras.yaml` bajo `motion_detection:`.

### `capture/camera_manager.py`
Lee `cameras.yaml`, crea una `CameraSource` por cada cámara con
`active: true`, les pasa la configuración de movimiento e
`inference_fps`, y expone `start_all()` / `stop_all()` / `get_status()`.

### `inference/model_loader.py`
Carga el modelo ONNX **una sola vez** (cacheado — nunca se recrea la
sesión por cámara ni por frame). Reusa `activar_directml()` de
`fisgon_amd.py` (el script de referencia original), que parchea
`onnxruntime.InferenceSession` para forzar `DmlExecutionProvider`.

Tiene dos protecciones agregadas durante el desarrollo, ambas por bugs
reales que aparecieron (ver sección 5):
- `ULTRALYTICS_SKIP_REQUIREMENTS_CHECKS=1`, seteada **antes** de importar
  `ultralytics`, para que nunca intente auto-instalar `onnxruntime` (el
  paquete CPU) y pise `onnxruntime-directml`.
- `_loguear_proveedor_real()`: después de cargar el modelo, fuerza una
  inferencia de prueba y consulta `session.get_providers()` directamente,
  logueando si la GPU está realmente activa. Esto existe porque el log
  que imprime `ultralytics` por su cuenta ("Using ONNX Runtime ... with
  CPUExecutionProvider") es **engañoso** — refleja lo que Ultralytics
  *pensaba* usar antes de que nuestro parche lo pisara, no lo que
  realmente se ejecutó.

### `inference/inference_worker.py`
Un único thread consume la cola compartida — nunca hay dos threads
llamando `modelo.predict()` a la vez sobre la misma sesión ONNX.

**Batching (agregado durante el desarrollo)**: en vez de procesar un
frame por llamada, cada vuelta del loop toma el primer frame disponible
(esperando si hace falta) y junta, sin esperar de más, todo lo que ya
esté esperando en la cola en ese momento (hasta `max_batch_size`, 4 por
defecto). Si solo hay 1 cámara activa o el worker va al día, el lote
termina siendo de 1 frame de cualquier forma — mismo comportamiento que
antes, sin penalidad. Ver sección 6 para por qué esto importa tanto.

### `events/event_validator.py`
Mantiene un historial corto de timestamps por (cámara, clase de objeto).
Confirma un evento real cuando hay al menos `min_consecutive_detections`
dentro de los últimos `window_seconds` — no es "detecto 1 y espero", es
una ventana deslizante: cada detección nueva chequea cuántas hubo
recientemente, y confirma apenas llega al mínimo. Tiene un cooldown para
no generar un evento nuevo en cada frame mientras el objeto siga en
cámara (uno cada `window_seconds`, no uno por frame).

Al confirmar, dibuja el bounding box + etiqueta sobre una copia del frame
(rectángulo rojo + texto con clase y confianza) antes de codificarlo a
JPEG/base64 — así el snapshot que llega a la nube muestra visualmente qué
detectó el modelo. También traduce el nombre de clase que usa el modelo
("gun") al que espera la API ("pistol") vía `CLASS_NAME_API_MAP`, y
convierte el bbox del formato `[x1,y1,x2,y2]` que da YOLO al formato
`{x,y,width,height}` que espera la API (ver sección 5, el contrato real se
descubrió por prueba y error contra la API).

### `events/local_buffer.py`
SQLite (`eventos.db`), para que los eventos sobrevivan a un reinicio del
proceso o un corte de luz — un evento solo se marca como subido después
de que `upload_worker` confirma el POST. Tiene `limpiar_antiguos()`
(agregado para 24/7, ver sección 7) que borra eventos ya subidos con más
de 7 días, sin tocar nunca los pendientes sin importar su antigüedad.

### `upload/upload_worker.py`
Thread separado. Cada `poll_interval_seconds` revisa el buffer local y
hace `POST {api_url}/events` con el payload de cada evento pendiente,
usando el header `X-Edge-Token` (no `Authorization: Bearer`, así lo
define la API real). Si falla, deja el evento pendiente y reintenta en el
próximo ciclo — nunca lo descarta.

Tiene una función `_asegurar_https()` que fuerza `https://` si alguien
pone `http://` en la config (ver sección 5, bug real que costó horas
diagnosticar). También corre `limpiar_antiguos()` una vez por día.

La URL de la API y el token salen de variables de entorno (`.env`, vía
`python-dotenv`, cargado en `main.py`): `FISGON_API_URL` y
`FISGON_EDGE_TOKEN`. Si no hay `.env`, usa `api.base_url` de
`cameras.yaml` como respaldo (útil para pruebas locales).

### `config_ui/app.py`
Interfaz web local (`localhost:8000`) para editar `cameras.yaml` y ver el
estado de las cámaras sin tocar el YAML a mano. **Reescrita durante el
desarrollo usando solo la librería estándar de Python** (`http.server`),
sin FastAPI/Pydantic — ver sección 5 para el motivo (bloqueo de Device
Guard en esta PC).

Funcionalidad:
- **Login obligatorio**: usuario/contraseña, sesión vía cookie
  (`HttpOnly`, token aleatorio, expira a las 12hs). La contraseña se
  guarda hasheada (PBKDF2 + salt) en `config_ui/auth_store.json`, nunca
  en texto plano — se bootstrapea con `admin`/`1234` la primera vez que
  corre, y se puede cambiar desde "Perfil" en la interfaz.
- **Edición de cámaras inline**: cada fila de la tabla tiene un botón
  "Editar" que convierte esa fila en inputs editables directamente (sin
  ir a un formulario aparte). El formulario de abajo es solo para dar de
  alta cámaras nuevas, y **rechaza un ID duplicado** con un mensaje claro
  en vez de pisar la cámara existente silenciosamente.
- **"Probar conexión"**: abre el RTSP (o el índice de webcam, si es un
  número) brevemente con `cv2.VideoCapture` para confirmar que conecta
  antes de guardar.

### `main.py`
Punto de entrada del pipeline. Carga `.env` (vía `load_dotenv()`), arranca
`camera_manager`, `inference_worker`, `event_validator` (encadenado como
callback), `local_buffer` y `upload_worker`, y maneja `Ctrl+C` para cerrar
todo ordenadamente. Logging con rotación diaria (`TimedRotatingFileHandler`,
14 días de retención — ver sección 7).

### `run.py`
Levanta `main.py` y `config_ui/app.py` como dos procesos separados (a
propósito — `config_ui` no sincroniza en caliente con el pipeline, los
cambios de `cameras.yaml` se aplican recién cuando se reinicia) pero con
un solo comando y un solo `Ctrl+C`. Tiene un **watchdog**: si cualquiera
de los dos procesos se cae solo, lo reinicia automáticamente a los 5
segundos. Si un proceso se cae 5 veces en 10 minutos, deja de reintentar
(evita un loop infinito de crashes si hay un problema real de fondo) — ver
sección 7.

### `fisgon_amd.py`
Script de referencia original del usuario (standalone, con ventana de
video en vivo). Se reusa su función `activar_directml()` en
`model_loader.py`. Sigue funcionando de forma independiente, útil para
pruebas visuales rápidas fuera del pipeline completo.

---

## 4. Decisiones de diseño y por qué

**Por qué Python y no C++/Qt/C#**: se evaluó explícitamente durante el
desarrollo (ver conversación completa). La conclusión, con datos: el
costo de CPU medido no viene del lenguaje — viene de código C++ dentro de
ONNX Runtime (el driver de DirectML), que es el mismo binario sin importar
qué lenguaje lo llame. Confirmado comparando el pipeline nuevo contra
`fisgon_amd.py` (Python también, pero código distinto): mismo orden de
magnitud de costo en los dos. Migrar no iba a resolver el cuello de
botella real, y hubiera costado semanas de reescritura con una fecha de
tesis fija en diciembre.

**Por qué no Servicio de Windows para 24/7**: los servicios corren en
"Sesión 0", un contexto históricamente con soporte inconsistente para
GPU/DirectX. En cambio: auto-login + Programador de Tareas al iniciar
sesión (mismo contexto que un usuario logueado corriendo el comando a
mano) — ver `INSTALACION.md`.

**Por qué SQLite local + API en la nube como fuente de verdad**: el
edge no necesita retener eventos para siempre — una vez subidos, la nube
es la referencia. Por eso `local_buffer` es un buffer de tránsito con
limpieza automática (7 días de retención de lo ya subido), no un
histórico permanente.

---

## 5. Bugs reales encontrados durante el desarrollo (y cómo se resolvieron)

Esta sección importa tanto como el resto — cada uno de estos costó tiempo
real de diagnóstico y es fácil de volver a pisar si no se sabe que existen.

### 5.1 — `onnxruntime` pisando `onnxruntime-directml` (pasó 3 veces)
`onnxruntime` (versión CPU) y `onnxruntime-directml` (versión GPU) se
registran bajo el **mismo nombre de import** (`onnxruntime`) pero son
paquetes de pip distintos. Instalar uno encima del otro deja el que se
instaló último activo, sin error visible. Pasó:
1. Al instalar `fastapi` por primera vez (arrastró un `onnxruntime` base).
2. Cuando `ultralytics` intentó auto-instalar `onnxruntime` porque no
   encontraba metadata de pip con ese nombre exacto (aunque
   `onnxruntime-directml` provee el mismo módulo). Arreglado con
   `ULTRALYTICS_SKIP_REQUIREMENTS_CHECKS=1` en `model_loader.py`.
3. Al reexportar el modelo con `ultralytics` para agregar soporte de
   batch — la misma causa que el punto 2, pero en un script standalone
   que no pasaba por `model_loader.py` y por lo tanto no tenía la
   protección.

**Lección**: cualquier uso de `ultralytics` fuera del pipeline (exportar,
reentrenar) tiene que setear esa variable de entorno primero, o va a
volver a pasar.

### 5.2 — Device Guard / Smart App Control bloqueando FastAPI/Pydantic
En esta PC, una política de seguridad bloquea la carga de extensiones
compiladas no firmadas — afectó a `pydantic_core` (usado por FastAPI).
Se probó con Pydantic v1 también, mismo bloqueo. Solución: reescribir
`config_ui/app.py` con `http.server` de la librería estándar, sin
dependencias de terceros con binarios compilados.

### 5.3 — Redirect HTTP→HTTPS perdiendo eventos silenciosamente
La URL de la API venía como `http://`. Cloud Run redirige automáticamente
a `https://` con un 302 — pero la librería `requests`, al seguir ese
redirect, **baja el método POST a GET** (comportamiento heredado de
compatibilidad con navegadores). Resultado: los eventos se mandaban
contra un `GET /events` que no existe → 404, sin ningún error claro del
lado nuestro. Arreglado en dos capas: URL cambiada a `https://`
directamente en `.env`/`cameras.yaml`, y una función `_asegurar_https()`
en `upload_worker.py` que fuerza el esquema correcto aunque alguien
vuelva a poner `http://` por error.

### 5.4 — El contrato real de la API no era el que se asumió
Se probó contra la API real con eventos de prueba (`camera_id:
cam_test_integracion`) y se descubrió, por prueba y error con los mensajes
de validación (422) que devuelve la API:
- `bbox` va como objeto `{x, y, width, height}`, no como lista
  `[x1,y1,x2,y2]`.
- Falta el campo `frames_validated` (ya lo calculábamos para el log, no
  lo mandábamos).
- `timestamp` no lo pide la API (lo asigna el servidor al recibir el
  evento) — se saca del payload de subida.

### 5.5 — `cap.set(CAP_PROP_FPS, ...)` rompe la webcam por completo
Se intentó pedirle a la webcam que capture directamente a 5fps (en vez de
decodificar a su ritmo nativo y descartar la mayoría) para bajar CPU.
Medido: con el backend MSMF de Windows, esto **rompe la captura
completamente** (0 frames reales, error `can't grab frame`). Con
`CAP_DSHOW` como backend, el pedido se "acepta" (`cap.get()` confirma 5.0)
pero no cambia nada en la práctica (sigue a ~18-28fps reales). Se
descartó esta vía — el código NO intenta cambiar el FPS de captura, hay
un comentario explicando esto en `camera_source.py`.

### 5.6 — IoBinding de ONNX Runtime no reduce el costo
Se sospechó que el overhead de ~45ms por llamada al modelo era por
reasignación de buffers en cada `session.run()`, y que IoBinding (reusar
buffers de GPU) lo reduciría. Medido: 48.98ms vs 49.41ms — sin diferencia
real. El overhead no es de asignación de memoria, es del despacho mismo
a DirectML (ver sección 6).

### 5.7 — Decode de video por hardware (GPU) no se activa realmente
Se probó forzar decode H.264 por hardware (`cv2.CAP_PROP_HW_ACCELERATION`
con `VIDEO_ACCELERATION_D3D11`) para bajar la carga de CPU de un stream
RTSP. La propiedad se "acepta" (reporta `2.0`), pero medido con el motor
de decode de video de la GPU (contador de Windows), el uso se mantuvo en
**0%** durante toda la prueba, y el decode resultó más lento que por
software. Se descartó.

### 5.8 — Caracteres especiales rompiendo el parser de PowerShell 5.1
El guión largo ("—") y las tildes en strings de un script `.ps1` pueden
romper el parseo en PowerShell 5.1 (el que trae Windows por defecto) si
el archivo no se lee con la codificación exacta esperada — el error que
tira el parser señala una línea completamente distinta a la real,
haciendo el diagnóstico confuso. Por eso `instalar.ps1` está escrito
deliberadamente sin tildes ni guiones largos.

### 5.9 — `numpy` demasiado nuevo rompe `ultralytics` sin mencionar a `numpy`
Al probar `instalar.ps1` de punta a punta en un entorno virtual nuevo
(distinto del entorno global de esta PC, donde todo ya venía funcionando),
`from ultralytics import YOLO` empezó a fallar con:

```
ImportError: cannot import name 'NP_SUPPORTED_MODULES' from 'torch._dynamo.utils'
```

El mensaje no menciona `numpy` para nada, y el error aparece adentro de
`ultralytics.models.sam` (código de soporte para SAM, que ni usamos) al
importar `torchvision`, que a su vez importa `torch._dynamo`. Se probó de
todo: reinstalar `torch` con la versión exacta del entorno que sí
funciona (`2.4.1`), instalar `torch-directml` también — nada lo arregló.

La causa real, encontrada comparando `pip freeze` del entorno que
funciona contra el que fallaba: **`numpy`**. El entorno que funciona tiene
`numpy==2.4.4`; un `pip install` nuevo sin fijar versión bajó `2.5.1` (más
nueva). `torch==2.4.1` internamente no reconoce esa versión de `numpy` al
armar `NP_SUPPORTED_MODULES`, y el import se rompe en cascada mucho más
abajo en la pila (en `torchvision`, no en `numpy` ni en `torch`
directamente), lejos de donde está la causa real. Se confirmó forzando
`pip install numpy==2.4.4` — arregla el problema al toque.

**Lección**: si algún día `ultralytics`/`torch` vuelven a romperse con un
error que no menciona a `numpy` para nada, sospechar de `numpy` primero.
Confirmar con `python -c "import torch._dynamo"` aislado (sin
`ultralytics` en el medio) — si eso solo ya falla, es este bug.
`requirements.txt` fija `numpy==2.4.4` explícitamente por esto.

---

## 6. La investigación de rendimiento (por qué importa tanto)

Este fue el trabajo más largo de la sesión, así que vale la pena dejarlo
documentado paso a paso — no solo el resultado, sino cómo se llegó.

**El síntoma**: con 1 sola cámara activa, el proceso usaba ~62-65% de la
capacidad total de CPU (6 núcleos) — mucho para "solo" tomar 5fps y
correrlos por un modelo que corre en GPU.

**Primera hipótesis (incorrecta)**: que el costo era decodificar la
webcam a su ritmo nativo (~28-30fps) para después usar solo 5 de esos
frames. Medido en aislado: la captura sola consume solo ~1% de CPU. Esta
hipótesis se descartó con datos — importante, porque la primera intuición
llevaba a optimizar la parte equivocada.

**Segunda hipótesis (correcta)**: el costo está en la llamada al modelo.
Confirmado aislando `modelo.predict()` sin cámara ni cola: a 5
llamadas/segundo, 64.5% de CPU — prácticamente idéntico al pipeline
completo.

**¿Por qué una llamada a un modelo que corre en GPU cuesta tanta CPU?**
Se perfiló con el profiler nativo de ONNX Runtime
(`enable_profiling=True`), que muestra tiempo real por nodo del grafo:
- El cómputo real en GPU (`SequentialExecutor::Execute`): 0.1 a 6.6ms.
- La llamada completa (`model_run`): 45 a 54ms.

La diferencia (~45ms) es overhead de CPU alrededor de la llamada —
transferencia de datos hacia/desde la GPU y despacho del driver de
DirectML — no cómputo. Se confirmó que esto **no es preprocesado ni NMS**
(eso es <5% del tiempo total, medido con `cProfile`), y que **no se
puede reducir con IoBinding** (sección 5.6) ni cambiando el proveedor.

**La solución no fue reducir el costo por llamada — fue reducir cuántas
llamadas hacen falta:**

1. **Filtro de movimiento** (`camera_source.py`): si la escena no cambia,
   no se llama al modelo. Con la escena estática, esto solo ya bajó el
   uso de CPU medido de ~65% a **17.2%** con 1 cámara.
2. **Batching** (`inference_worker.py`): si hay varias cámaras, se
   procesan juntas en una sola llamada al modelo en vez de una por
   cámara — el overhead fijo de ~45ms se paga una vez, no N veces.
   Medido: 4 llamadas individuales = 46.4ms/frame. 1 llamada con 4 frames
   juntos = 28.0ms/frame. En CPU sostenida, simulando 4 cámaras a 5fps:
   **73.3%** con batching contra una proyección de **~258%** sin
   batching — sin esto, 4 cámaras reales muy probablemente hubieran
   saturado la máquina.

Esto requirió reexportar el modelo (`models/best.onnx`) con soporte de
batch dinámico (antes estaba fijo a procesar de a 1 frame) — al hacerlo se
volvió a pisar `onnxruntime-directml` (bug 5.1, punto 3), reparado antes
de seguir.

**Comparación con tecnologías alternativas** (Qt/C++, C#, Rust): dado que
el costo real vive dentro de ONNX Runtime (C++ compilado, mismo binario
sin importar el lenguaje que lo llame), migrar de lenguaje no iba a
reducir este costo — confirmado comparando el pipeline contra
`fisgon_amd.py` (mismo lenguaje, código distinto, mismo orden de
magnitud de costo). El filtro de movimiento y el batching funcionarían
igual de bien o mal en cualquier lenguaje, porque atacan *cuántas veces y
cómo* se llama a ONNX Runtime, no quién lo llama.

---

## 7. Confiabilidad para correr 24/7

Pensado para una tesis con defensa en diciembre — el sistema tiene que
poder quedar corriendo sin supervisión por semanas. Cuatro cosas
agregadas específicamente para esto (ninguna depende del lenguaje, ver
sección 6 sobre por qué eso no se resuelve migrando):

1. **Rotación de logs** (`main.py`): `logs/edge.log` usa
   `TimedRotatingFileHandler` — rota a medianoche, guarda 14 días, borra
   el resto solo. Sin esto, el archivo crece sin límite.
2. **Limpieza de `eventos.db`** (`local_buffer.py` +
   `upload_worker.py`): una vez por día, borra eventos ya subidos con
   más de 7 días y compacta el archivo (`VACUUM`). Nunca toca eventos
   pendientes, sin importar su antigüedad — verificado con un test
   explícito de 3 casos (viejo+subido se borra, reciente+subido
   sobrevive, viejo+pendiente sobrevive).
3. **Watchdog de reinicio automático** (`run.py`): vigila ambos procesos
   (pipeline y panel web) cada segundo; si alguno se cae, lo reinicia a
   los 5 segundos. Probado matando `main.py` a la fuerza mientras corría:
   volvió a levantar solo, con modelo cargado, GPU confirmada y cámara
   reconectada, sin ninguna intervención manual. Si un proceso se cae 5
   veces en 10 minutos, deja de reintentar (para no quedar en loop
   infinito si hay un problema real de fondo que no se arregla solo).
4. **Prueba de memoria en corrida larga**: se corrió un soak test
   muestreando el uso de RAM del proceso cada 60 segundos. En la ventana
   observada, la memoria se mantuvo estable (sin tendencia de
   crecimiento sostenido) — ver `logs/memoria_soak.csv` para los datos
   crudos. Esto fue una ventana corta (minutos, no 24hs reales); para
   confianza completa hace falta dejarlo corriendo un período largo real
   y volver a revisar ese archivo.

---

## 8. Despliegue en otra máquina

Ver `INSTALACION.md` para la guía paso a paso. Puntos clave, en resumen:

- **Copiar la carpeta no alcanza por sí solo**: el entorno de Python
  (intérprete + paquetes instalados, en particular `onnxruntime-directml`)
  no vive dentro de la carpeta del proyecto — vive en la instalación de
  Python de la máquina. `instalar.ps1` arma un entorno virtual nuevo y
  **verifica de verdad** que la GPU esté activa antes de terminar (no
  solo que pip haya dicho "listo").
- **Dónde poner la carpeta**: evitar `Escritorio`/`Documentos` (riesgo de
  sincronización automática con OneDrive, que subiría credenciales y
  snapshots de detecciones a la nube sin que nadie lo decida
  explícitamente) y `Program Files` (permisos/UAC). Recomendado: raíz de
  un disco interno fijo.
- **No como Servicio de Windows**: riesgo real de que DirectML no
  inicialice igual en Sesión 0. En cambio: auto-login + Programador de
  Tareas al iniciar sesión — mismo contexto que un usuario logueado.
- **`requirements.txt` con versiones fijadas** (no rangos abiertos): para
  que una instalación nueva no resuelva una versión distinta de
  `onnxruntime` y vuelva a pisar la variante DirectML.

---

## 9. Qué queda pendiente / próximos pasos

- **Batching con cámaras reales**: se validó el mecanismo (agrupa
  correctamente, atribuye detecciones a la cámara correcta) simulando
  varias cámaras sobre la única webcam disponible. Falta confirmar con
  el DVR real conectado con más de una cámara a la vez.
- **Substream del DVR**: si en algún momento se puede configurar el DVR
  (hoy no hay acceso para eso), usar un substream de menor
  resolución/fps para los canales de análisis reduciría la carga de
  decode del lado del DVR — ver la discusión sobre RTSP en esta misma
  documentación (sección de decisiones).
- **Soak test de memoria largo**: la prueba corrida fue de minutos, no
  de 24 horas reales. Revisar `logs/memoria_soak.csv` después de una
  corrida larga real antes de confiar en el número para el despliegue
  final.
- **Múltiples snapshots por evento**: la API hoy solo acepta un
  `snapshot_base64` por evento. Si el backend agrega soporte para varias
  imágenes por evento, se puede conectar guardando los frames de la
  ventana de detección en vez de solo el que confirmó el evento.
- **Cambiar la contraseña por defecto** del panel (`admin`/`1234`) antes
  de la demo/defensa — se cambia desde "Perfil" en la interfaz web.
