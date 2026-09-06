# Cupo de cámaras por edge — trabajo del backend

Spec para implementar en la API. Autocontenido: no hace falta conocer el
código del edge para trabajar con esto.

---

## 1. Contexto

Fisgon es un sistema de detección de armas. Cada cliente tiene un **edge**: una
PC en su local que corre el pipeline (cámaras → modelo YOLO → eventos) y sube
las alertas a esta API.

Un edge se autentica con un token propio que se le entrega al instalarlo. Va en
el header `X-Edge-Token` de cada request:

```http
POST /events
X-Edge-Token: fisgon_edge_xxxxxxxxxxxxxxxxxxxx
Content-Type: application/json

{
  "camera_id": "cam_1",
  "object_class": "pistol",
  "confidence": 0.87,
  "bbox": { "x": 120, "y": 64, "width": 210, "height": 180 },
  "frames_validated": 3,
  "snapshot_base64": "..."
}
```

Los endpoints que el edge ya consume hoy son `POST /events` y
`POST /events/{id}/video`.

**Lo que falta:** cada cliente tiene contratada una cantidad máxima de cámaras,
y hoy nada en el sistema lo sabe. Un edge puede levantar 20 cámaras sin que la
nube se entere.

## 2. La decisión de diseño, y por qué

**El cupo se guarda en el backend, asociado al edge, y se consulta con el
token.** No en un archivo de configuración del edge.

Tres razones:

1. **Cambiar el plan no requiere entrar a la máquina del cliente.** Si el número
   viviera en el edge, pasar un cliente de 4 a 8 cámaras significaría coordinar
   un acceso remoto a su PC para editar un archivo.
2. **Un archivo local lo edita cualquiera.** El cupo es un dato comercial; si
   vive en la máquina del cliente es una sugerencia, no un límite.
3. **El canal ya existe.** El edge ya habla con la API autenticado en cada
   evento.

**El edge no es un punto de control confiable.** Corre en hardware del cliente,
así que cualquier validación que haga es evitable parcheando el código. El edge
va a aplicar el cupo igual (para no arrancar cámaras de más y para avisarle al
operador), pero eso es *buena conducta*, no una barrera. **La única barrera real
es la de esta API.**

---

## 3. Qué hay que construir

### 3.1 Guardar el cupo — `max_cameras`

Un campo nuevo en el modelo donde hoy vive el edge / la credencial:

| Campo | Tipo | Reglas |
|---|---|---|
| `max_cameras` | entero | `>= 1`. Obligatorio al crear el edge. Sin default silencioso. |

> **Nota:** no conozco el modelo actual de edges/credenciales, así que no puedo
> decir en qué tabla va. Donde hoy se guarda el token es el lugar correcto.

**Que sea obligatorio al crear importa.** Si admite `NULL` o toma un default,
en algún momento va a haber edges sin cupo definido y nadie va a saber si es un
plan ilimitado o un alta mal hecha.

### 3.2 `GET /edge/config` — que el edge pueda consultarlo

El endpoint que el edge llama al arrancar.

```http
GET /edge/config
X-Edge-Token: fisgon_edge_xxxxxxxxxxxxxxxxxxxx
```

**200 OK**

```json
{
  "edge_id": "sacme-01",
  "max_cameras": 4,
  "plan": "standard"
}
```

- `max_cameras` — **el único campo obligatorio.** Entero.
- `edge_id`, `plan` — opcionales. El edge los muestra en su interfaz local para
  que el instalador confirme que la PC quedó asociada al cliente correcto.

**401** si el token es inválido o falta. Mismo comportamiento que en `/events`.

Es un endpoint de solo lectura: el edge nunca escribe su propio cupo.

**Cadencia:** el edge lo consulta en cada arranque del pipeline, que ocurre al
levantar el sistema y cada vez que se guarda un cambio de configuración en su
interfaz local. No es un endpoint de alto tráfico — del orden de unas pocas
llamadas por día por edge.

### 3.3 Alta y edición desde el panel de administración

El panel de administración (frontend web) tiene que poder **fijar el cupo al
crear las credenciales de un edge** y **modificarlo después**. Eso requiere del
lado de la API:

- El endpoint que hoy **crea** un edge / sus credenciales debe **aceptar y
  exigir** `max_cameras`.
- Debe existir una forma de **modificarlo** sin recrear el edge ni rotarle el
  token (un `PATCH`, o el update que ya exista). Cambiar el plan de un cliente
  no puede implicar reinstalarle el sistema.
- Los endpoints de listado/detalle deben **devolver** `max_cameras`, para que el
  panel lo muestre sin adivinarlo.

Hay un documento aparte con el detalle del lado del frontend
(`CUPO_CAMARAS_FRONTEND.md`); acordá con esa persona los nombres exactos de los
campos antes de implementar.

### 3.4 Rechazar eventos fuera de cupo — la barrera real

Esta es la parte que hace que el cupo signifique algo. Se puede entregar
después de lo anterior, pero sin esto el límite es voluntario.

Cuando llega un `POST /events` de una cámara que excede el cupo del edge, la API
lo rechaza:

```json
{ "detail": "camera_out_of_quota" }
```

Con un **código HTTP distinto del 401**. Sugerencia: `403`.

**Por qué tiene que ser distinguible:** el edge reintenta los eventos que fallan
por red y los conserva en su buffer local para no perder alertas. Necesita poder
distinguir tres casos:

| Respuesta | Qué hace el edge |
|---|---|
| Error de red / 5xx | Reintenta indefinidamente, no descarta |
| 401 token inválido | Loguea "revisar credenciales" |
| 403 fuera de cupo | Loguea "esta cámara excede el plan" y deja de reintentarla |

Si los tres devuelven lo mismo, un edge mal configurado reintenta para siempre
una cámara que nunca va a ser aceptada, y el log no ayuda a diagnosticar.

**Cómo decidir qué cámara está fuera de cupo:** hay que definirlo (ver sección
5). La opción más simple es registrar las primeras `max_cameras` `camera_id`
distintas que reporta cada edge y rechazar las siguientes.

---

## 4. Cómo se comporta el edge — para que no sorprenda

El edge está diseñado para **fallar hacia abierto**: si no puede averiguar su
cupo, arranca todas las cámaras y lo deja anotado en su log.

| Situación | Qué hace el edge |
|---|---|
| La API responde | Aplica el cupo y lo cachea en disco |
| No responde, hay cache | Aplica el último cupo conocido |
| No responde, sin cache | Arranca todas las cámaras + WARNING |
| **404 (endpoint no desplegado)** | Igual que "no responde" |
| 401 | Arranca todas + WARNING |

Dos consecuencias que conviene tener presentes:

- **El 404 es un camino soportado a propósito.** El edge se puede desplegar
  antes que este endpoint exista y no se rompe. Cuando `/edge/config` aparezca,
  los edges empiezan a aplicar el cupo sin cambiarles una línea.
- **Un corte de red no puede apagar cámaras.** Es un sistema de vigilancia: el
  riesgo de que un cliente corra dos cámaras de más durante una caída es mucho
  menor que el de un sistema que se ciega solo. Por eso la barrera de esta API
  (sección 3.4) es la que realmente sostiene el límite.

---

## 5. A definir antes de implementar

Tres cosas que no se pueden decidir de un solo lado:

1. **¿El cupo cuenta cámaras o canales de DVR?**
   En el edge, cada cámara es una URL RTSP. Un DVR de 16 canales se carga hoy
   como 16 entradas. Hay que fijar cuál es la unidad que se factura.

2. **¿`camera_id` es único por edge o global?**
   Hoy lo elige el instalador en el edge (`cam_1`, `cam_2`, ...), así que **dos
   edges distintos pueden tener `cam_1`**. Si la API va a llevar la cuenta de
   cámaras por edge, la clave tiene que ser compuesta: `(edge, camera_id)`.

3. **¿Qué pasa si un cliente baja de plan teniendo más cámaras activas?**
   Propuesta desde el edge: la API devuelve el cupo nuevo, el edge apaga las
   últimas del archivo y lo deja visible en su interfaz para que el operador
   elija cuáles conservar. Del lado de la API habría que decidir si las cámaras
   ya registradas se dan de baja automáticamente o quedan marcadas.
