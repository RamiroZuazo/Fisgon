# Instalación del edge Fisgon en una máquina nueva

Esta guía asume que ya tenés la carpeta del proyecto (copiada desde la máquina
de desarrollo, un pendrive, etc.) en la máquina destino. Si no sabés qué
copiar, ver la sección **"Qué copiar"** más abajo.

## 1. Prerrequisitos

- **Windows** (no se soporta Linux/Mac para este proyecto — ver `CONTEXT.md`).
- **Python 3.12** instalado y en el PATH (`python --version` debe mostrar
  `3.12.x`). Bajarlo de [python.org](https://www.python.org/downloads/) si no
  está. Tiene que ser exactamente 3.12: la rueda de `onnxruntime-directml`
  que usamos (`1.24.4`) está compilada específicamente para esa versión.
- **GPU AMD compatible con DirectML**, con drivers actualizados. Sin esto la
  inferencia cae a CPU (mucho más lenta, ver `CONTEXT.md` para el
  benchmark de referencia).
- **Acceso de red** a las cámaras RTSP (o una webcam local para pruebas) y a
  internet (para subir eventos a la API en la nube).

## 2. Dónde poner la carpeta

- **Evitar** `Escritorio` o `Documentos` dentro de `C:\Users\...` — Windows
  suele sincronizar esas carpetas con OneDrive automáticamente, lo que
  subiría el `.env` (con el token de la API), `cameras.yaml` (con
  credenciales RTSP) y `eventos.db` (con snapshots reales de detecciones) a
  la nube de Microsoft sin que te des cuenta.
- **Evitar** `C:\Program Files\` — Windows protege esa ruta con permisos de
  administrador y puede "virtualizar" las escrituras de una app sin
  permisos elevados, dando comportamiento confuso (la app escribe logs, la
  base SQLite, etc.).
- **Recomendado**: una carpeta simple en la raíz de un disco interno fijo,
  por ejemplo `C:\Fisgon\`. No uses una unidad de red ni un pendrive para
  la instalación real (para un sistema que corre 24/7, que alguien
  desconecte el disco por error es un riesgo real).

## 3. Qué copiar de la máquina de desarrollo

Copiá la carpeta completa **excepto**:
- `venv\` (si existe) — se genera de nuevo en la máquina destino, no tiene
  sentido copiarlo (además puede pesar varios GB).
- `__pycache__\` (si existe) — se regenera solo.
- `logs\` — dejalo vacío, se regenera solo.

**Asegurate de incluir** (son fáciles de olvidar porque no son código):
- `models\best.onnx` (y `best.pt` si lo vas a necesitar para reexportar) —
  pesan ~100MB y ~50MB respectivamente, no son chicos.
- `.env` — tiene las credenciales de la API. Si no lo tenés a mano, lo
  podés crear directo en la máquina destino (ver paso 5).
- `cameras.yaml` — la configuración de cámaras. Si vas a apuntar a cámaras
  distintas en la máquina nueva, más simple armar uno nuevo ahí (paso 6).

## 4. Correr el instalador

Abrí PowerShell, parate en la carpeta del proyecto, y corré:

```powershell
.\instalar.ps1
```

Si PowerShell se queja de política de ejecución de scripts, corré esto una
vez (solo afecta la sesión actual, no cambia nada permanente):

```powershell
powershell -ExecutionPolicy Bypass -File .\instalar.ps1
```

El script:
1. Verifica que haya Python 3.12.
2. Crea un entorno virtual en `venv\` (aislado del Python del sistema, para
   no pisar nada de otras cosas que tengas instaladas en esa PC).
3. Instala las dependencias exactas de `requirements.txt`.
4. Crea las carpetas `logs\` y `models\` si no existen.
5. Chequea que `.env`, `cameras.yaml` y `models\best.onnx` estén presentes.
6. **Verifica de verdad que la GPU esté activa** — no solo que
   `onnxruntime-directml` esté instalado, sino que el modelo realmente
   corra por `DmlExecutionProvider`. Si dice que corre por CPU, revisá los
   drivers de AMD antes de seguir.

Si algo falla, el script se detiene con un mensaje en rojo explicando qué
pasó — no sigue instalando a medias.

## 5. Configurar credenciales (`.env`)

Si no viene copiado, creá un archivo `.env` en la raíz del proyecto con:

```
FISGON_API_URL=https://tu-api-en-la-nube.com
FISGON_EDGE_TOKEN=el-token-de-este-edge
```

Estos valores los tiene que dar el equipo de backend — identifican a este
edge específico frente a la API. **Nunca** los pongas en `cameras.yaml` ni
en el código: `.env` es el único lugar donde deben vivir (ver
`upload/upload_worker.py` para el detalle de cómo se usan).

También se pueden cargar desde la interfaz web (Ajustes → "API en la nube"),
que escribe en este mismo `.env` y reinicia el pipeline para aplicarlo — útil
para no editar el archivo a mano en la máquina del cliente.

## 6. Configurar las cámaras (`cameras.yaml`)

Opción A — a mano: editá `cameras.yaml` con la URL RTSP real de cada
cámara/canal del DVR. Formato:

```yaml
cameras:
  - id: cam_1
    name: "Entrada principal"
    rtsp_url: "rtsp://usuario:password@192.168.1.22:554/Streaming/Channels/101"
    active: true
```

Opción B — desde el panel web: arrancá `config_ui\app.py` (ver paso 7),
entrá a `http://localhost:8000` con las credenciales del panel
(`admin` / `1234` por defecto — cambiala desde "Perfil" apenas entres), y
agregá las cámaras ahí. Tiene un botón "Probar conexión" para confirmar
que cada URL RTSP conecta antes de guardar.

## 7. Correr el sistema

Con el entorno virtual ya armado por `instalar.ps1`:

```powershell
venv\Scripts\python.exe run.py --config cameras.yaml
```

Esto levanta todo junto: cámaras, inferencia, validación de eventos, y la
interfaz web en `http://localhost:8000`. `run.py` además vigila ambos
procesos y los reinicia solo si alguno se cae (ver `SISTEMA.md`, sección
"Watchdog", para el detalle).

Para cerrarlo: `Ctrl+C` en esa consola.

## 8. Dejar que arranque solo al prender la PC

**No lo armamos como Servicio de Windows a propósito.** Los servicios
corren en un contexto aislado ("Sesión 0") que históricamente tiene
soporte inconsistente para GPU/DirectX — hay riesgo real de que la
inferencia caiga a CPU sin avisar, silenciosamente, corriendo como
servicio. En cambio, usamos:

1. **Auto-login de Windows** para la cuenta que va a correr el sistema
   (`netplwiz` → destildar "Los usuarios deben escribir su nombre..." →
   seguir el asistente). Esto hace que la PC entre directo al escritorio
   al prender, sin esperar que alguien inicie sesión a mano.
2. **Programador de Tareas** con una tarea nueva:
   - Desencadenador: "Al iniciar sesión".
   - Acción: iniciar programa `venv\Scripts\python.exe`, argumentos
     `run.py --config cameras.yaml`, "Iniciar en" apuntando a la carpeta
     del proyecto.
   - En Configuración: marcar "Reiniciar la tarea si error" (capa extra
     de recuperación además del watchdog propio de `run.py`).

Este approach corre en el mismo contexto que si vos ejecutaras el comando a
mano después de loguearte — mismo acceso a GPU, sin sorpresas.

## 9. Problemas conocidos (ya nos pasaron durante el desarrollo)

**"El modelo corre por CPU, no por GPU"** (verificado en el paso 6 del
instalador, o viendo `logs\edge.log` buscar la línea
`Inferencia corriendo por GPU`): casi siempre es que algo reinstaló
`onnxruntime` (sin `-directml`) encima, pisando el proveedor DirectML.
Arreglo:

```powershell
venv\Scripts\python.exe -m pip uninstall -y onnxruntime onnxruntime-directml
venv\Scripts\python.exe -m pip install onnxruntime-directml==1.24.4
```

Esto pasa típicamente si alguien corre `ultralytics` para exportar o
reentrenar el modelo sin la variable de entorno de protección puesta —
ver `SISTEMA.md`, sección 5 ("Bugs reales encontrados durante el
desarrollo") para el detalle completo de por qué pasa esto.

**`ImportError: cannot import name 'NP_SUPPORTED_MODULES' from
'torch._dynamo.utils'`** (puede aparecer al importar `ultralytics`,
aunque el mensaje no lo diga): es `numpy` en una versión demasiado nueva
para el `torch` fijado (`2.4.1`). `requirements.txt` ya fija
`numpy==2.4.4` para evitar esto, pero si alguien instaló algo a mano
después y lo pisó, arreglo:

```powershell
venv\Scripts\python.exe -m pip install numpy==2.4.4
```

Ver `SISTEMA.md`, sección 5.9, para el detalle completo de cómo se
encontró este bug (fue engañoso: el error no menciona `numpy` para
nada).

**Los eventos no se suben a la nube**: revisá que `.env` tenga
`FISGON_API_URL` (con `https://`, no `http://`) y `FISGON_EDGE_TOKEN`
correctos, y que la PC tenga salida a internet. `logs\edge.log` va a
mostrar el motivo exacto del rechazo (código HTTP + mensaje de la API).

**El panel web (`localhost:8000`) no abre**: confirmá que
`config_ui\app.py` esté corriendo (si usaste `run.py`, ya lo levanta
junto con el pipeline). Si corriste `run.py --sin-ui`, el panel no se
levanta a propósito.

**Windows bloquea la instalación de algún paquete** ("Device Guard" /
"Smart App Control" / "acceso denegado"): esto es una política del equipo
específico, no del proyecto. Si te topás con esto, avisá — hay una
solución alternativa documentada para el panel de configuración (ver
`SISTEMA.md`, sección sobre `config_ui`).
