# Pulso de vida del edge — trabajo del backend

Spec para implementar en la API. Autocontenido: no hace falta conocer el código
del edge. **El lado del edge ya está implementado y funcionando**; falta el
endpoint que reciba los pulsos.

---

## 1. El problema

Fisgon es un sistema de detección de armas. Cada cliente tiene un **edge**: una
PC en su local que corre el pipeline (cámaras → modelo → alertas) y sube los
eventos a esta API, autenticándose con `X-Edge-Token`.

Hoy **la única señal que la nube tiene de un edge son los eventos**. Eso
significa que un local tranquilo, sin ninguna alerta en tres días, es
indistinguible de un local donde el sistema se cayó el lunes y nadie lo notó.
Tampoco hay forma de saber si una de las seis cámaras del cliente se
desconectó.

El edge ahora manda un **pulso de vida** periódico con el estado de cada
cámara. Falta recibirlo, guardarlo y exponerlo.

---

## 2. Qué hay que construir

### 2.1 `POST /edge/heartbeat`

```http
POST /edge/heartbeat
X-Edge-Token: fisgon_edge_xxxxxxxxxxxxxxxxxxxx
Content-Type: application/json
```

Este es un cuerpo **real**, capturado del edge corriendo:

```json
{
  "uptime_seconds": 3600,
  "cameras": [
    {
      "camera_id": "cam_3",
      "name": "Tiandy 2",
      "status": "connected",
      "seconds_since_last_frame": 0.4,
      "seconds_since_last_inference": 1.2,
      "frames_processed": 1834
    },
    {
      "camera_id": "cam_2",
      "name": "CASA1",
      "status": "failed",
      "seconds_since_last_frame": null,
      "seconds_since_last_inference": null,
      "frames_processed": 0
    },
    {
      "camera_id": "cam_1",
      "name": "Webcam de prueba (PC)",
      "status": "disabled",
      "seconds_since_last_frame": null,
      "seconds_since_last_inference": null,
      "frames_processed": 0
    }
  ]
}
```

**Respuesta esperada:** cualquier 2xx. El edge no usa el cuerpo de la
respuesta.

**Campos:**

| Campo | Tipo | Significado |
|---|---|---|
| `uptime_seconds` | entero | Hace cuánto arrancó el pipeline |
| `camera_id` | string | Id local de la cámara, lo elige el instalador |
| `name` | string \| null | Nombre que le puso el instalador |
| `status` | string | Ver tabla de estados abajo |
| `seconds_since_last_frame` | número \| null | Hace cuánto llegó video de esa cámara. `null` = nunca desde que arrancó |
| `seconds_since_last_inference` | número \| null | Hace cuánto un frame suyo pasó por el modelo |
| `frames_processed` | entero | Acumulado desde que arrancó el pipeline (se reinicia con él) |

**Se reportan todas las cámaras configuradas, corran o no.** Una cámara
`disabled` no está caída: el operador la desactivó. Esa distinción es lo que
permite mostrar algo útil en la web.

### 2.2 Los siete estados

Hay que guardarlos como vienen; la traducción a lenguaje de usuario es del
frontend.

| `status` | Qué significa |
|---|---|
| `connected` | Conectada y llegando video |
| `connecting` | Intentando conectar por primera vez |
| `reconnecting` | Estaba conectada, se cortó, está reintentando |
| `failed` | Falló la conexión y está en backoff (reintenta cada vez más espaciado) |
| `stopped` | El pipeline la tiene, pero su hilo no está corriendo |
| `disabled` | Desactivada por el operador en la config del edge |
| `out_of_quota` | Activa en config pero excede el cupo del plan, así que no se levantó |

### 2.3 Qué guardar

Por edge:

- **`last_heartbeat_at`, con el reloj del servidor.** No con un timestamp que
  mande el edge (ver sección 3).
- `uptime_seconds` del último pulso.

Por cámara (clave `(edge, camera_id)`):

- `name`, `status`, `frames_processed`.
- **El instante absoluto del último frame**, calculado al recibir:
  `last_frame_at = ahora_servidor - seconds_since_last_frame`. Guardar el
  número relativo tal cual serviría de poco: envejece solo.
- Ídem `last_inference_at`.

Conviene conservar el historial de cámaras vistas aunque desaparezcan de un
pulso: si el operador borra una cámara del edge, deja de venir en el payload, y
la web quiere poder mostrar "vista por última vez el 3 de septiembre".

### 2.4 Exponerlo para la web

El panel necesita, por cada edge: `last_heartbeat_at` y la lista de cámaras con
`name`, `status`, `last_frame_at` y `last_inference_at`.

Si ya existe un endpoint de detalle del edge, sumarlo ahí. El documento del
frontend (`PULSO_VIDA_FRONTEND.md`) describe qué se muestra; **acordá con esa
persona la forma exacta antes de implementar.**

### 2.5 Detectar un edge caído

**Esto no llega como un evento: llega como una ausencia.** Cuando el edge se
apaga, deja de mandar pulsos y nada más pasa.

Alguien tiene que comparar `last_heartbeat_at` contra el reloj del servidor y
decidir que un edge está caído. Se puede resolver de dos formas:

- **En la lectura** (más simple): el endpoint devuelve `last_heartbeat_at` y la
  web calcula. Sirve para mostrar, no para alertar.
- **Con un job periódico** (si se quiere avisar): marca los edges sin pulso y
  dispara la notificación.

Umbral sugerido: **tres intervalos perdidos** (~15 minutos con la cadencia por
defecto). Uno solo daría falsos positivos por cualquier microcorte de internet.

---

## 3. Dos decisiones del edge que conviene entender

**1. Manda antigüedades en segundos, no timestamps.**
El edge corre en la PC del cliente y su reloj puede estar mal — no es
hipotético, es una PC de local comercial que quizás nunca sincronizó hora. Si
mandara timestamps absolutos, la web podría mostrar "última señal hace 3 días"
sin que pase absolutamente nada. Mandando "hace 0.4 segundos", la nube convierte
con **su** reloj, que sí es confiable. Por eso el cálculo de la sección 2.3 se
hace al recibir el pulso, no después.

**2. El pulso no se reintenta ni se bufferea.**
A diferencia de los eventos —que el edge guarda en SQLite y reintenta para
siempre— un pulso que falla se descarta. Un pulso viejo no describe nada: el
siguiente lo reemplaza entero. Y que un pulso no llegue *es* la señal de que
algo anda mal, así que no habría que enmascararlo con reintentos.

**Consecuencia para la API:** los pulsos pueden llegar con huecos sin que eso
signifique que el edge estuvo caído todo ese tiempo. Un hueco de un intervalo
es ruido de red; varios seguidos son una señal.

---

## 4. Volumen

Un pulso cada **5 minutos por defecto** (configurable en el edge, con un piso
de 30 segundos). Con 100 edges son ~1200 requests por hora: nada que requiera
tratamiento especial, pero es escritura constante, así que conviene que sea un
`UPDATE` sobre una fila por edge y no un insert de historial sin límite.

Si se quiere historial para graficar disponibilidad, es una decisión aparte y
va a necesitar una política de retención.

---

## 5. A definir

1. **¿El pulso debe alertar o solo mostrarse?** Si un edge caído tiene que
   generar una notificación, hace falta el job de la sección 2.5 y decidir a
   quién se le avisa.

2. **¿Se guarda historial de estados o solo el último?** Solo el último alcanza
   para "¿está andando ahora?". Para "¿cuánto estuvo caída esta cámara el mes
   pasado?" hace falta historial.

3. **¿`camera_id` es único por edge o global?** Lo elige el instalador en el
   edge (`cam_1`, `cam_2`, ...), así que **dos edges distintos pueden tener
   `cam_1`**. La clave tiene que ser compuesta: `(edge, camera_id)`.
