# Pulso de vida del edge — trabajo del panel web

Spec para el frontend. Autocontenido: no hace falta conocer el código del edge
ni de la API.

---

## 1. Qué se quiere resolver

Cada cliente tiene un **edge**: una PC en su local que corre la detección sobre
sus cámaras y manda las alertas a la nube.

Hoy, desde la web, **no hay forma de saber si un sistema está funcionando**. Un
local sin alertas en tres días puede ser un local tranquilo o un local donde el
sistema se apagó el lunes. Tampoco se sabe si una de las seis cámaras del
cliente se desconectó.

El edge ahora manda un **pulso de vida cada 5 minutos** con el estado de cada
cámara. Falta mostrarlo.

**El objetivo concreto:** que se pueda entrar y ver, por cliente, qué cámaras
están en línea, cuáles se cayeron y desde cuándo.

---

## 2. Qué mostrar

### 2.1 Estado del edge

Antes que el detalle de las cámaras, si el sistema del cliente está vivo. Sale
de `last_heartbeat_at`, comparado contra ahora:

| Antigüedad del último pulso | Cómo mostrarlo |
|---|---|
| Menos de ~15 min | **En línea** |
| Más de ~15 min | **Sin señal desde hace {tiempo}** |
| Nunca hubo pulso | **Nunca reportó** |

El umbral son tres pulsos perdidos. Con uno solo habría falsos positivos por
cualquier microcorte de internet del local.

### 2.2 Estado de cada cámara

Por cada cámara: `name` (el que le puso el instalador), su estado, y hace cuánto
llegó video. La API manda siete estados posibles; esta es la traducción
sugerida:

| `status` | Texto para el usuario | Lectura |
|---|---|---|
| `connected` | **En línea** | bien |
| `connecting` | **Conectando…** | transitorio |
| `reconnecting` | **Reconectando…** | transitorio |
| `failed` | **Sin conexión** | problema |
| `stopped` | **Detenida** | problema |
| `disabled` | **Desactivada** | neutro, no es un error |
| `out_of_quota` | **Fuera del plan** | neutro, es una decisión comercial |

**Los últimos dos no son fallas y no deberían pintarse de rojo.** `disabled` es
una cámara que el operador apagó a propósito. `out_of_quota` es una cámara
cargada en el edge que excede las cámaras contratadas, así que el sistema no la
levanta. Si se muestran como caídas, alguien va a ir a un local a arreglar algo
que funciona como debe.

Junto al estado, **hace cuánto llegó su último frame** (`last_frame_at`):
"último video hace 3 segundos". Es lo que distingue una cámara realmente viva de
una que figura conectada pero hace veinte minutos que no manda nada.

---

## 3. Tres cosas que es fácil hacer mal

### 3.1 Sin pulso no es "todo bien"

**Si el edge no reportó, el estado de las cámaras es desconocido, no el último
que se supo.** Los datos que tenés son de hace media hora: la cámara puede
haberse caído hace 25 minutos.

Cuando el edge está sin señal, mostrar sus cámaras en gris/desconocido, no con
el último estado como si fuera actual. Es la diferencia entre "no sé" y una
afirmación falsa. En un sistema de seguridad, un panel que dice "todo en orden"
con datos viejos es peor que uno que dice "no tengo información".

### 3.2 Mostrar siempre la antigüedad del dato

Un cartel de "En línea" sin fecha invita a creer que es de ahora. Poner
siempre **"actualizado hace X"** junto al estado, tomado de
`last_heartbeat_at`.

### 3.3 "En línea" no es lo mismo que "detectando"

El edge no procesa todos los frames: cuando no hay movimiento baja el ritmo
para no gastar CPU. Por eso una cámara puede estar `connected` con video
llegando y aun así tener un `last_inference_at` de hace un rato — está bien,
significa que no pasa nada delante de esa cámara.

**No presentar `last_inference_at` como si fuera un problema.** Si se muestra,
que sea como dato secundario ("última vez analizada"), no como una alarma.

---

## 4. Dónde ponerlo

Dos lugares, según a quién le sirve:

- **En el listado de clientes**, una columna o un indicador con el estado del
  edge y algo tipo `4/5 cámaras` — para barrer de un vistazo quién tiene un
  problema.
- **En el detalle del cliente**, la tabla completa de cámaras con estado,
  último video y última vez analizada.

---

## 5. Lo que hay que acordar con backend

El trabajo depende de que la API exponga estos datos. Hay un documento paralelo
(`PULSO_VIDA_BACKEND.md`) donde ya está pedido:

- guardar `last_heartbeat_at` con el reloj del servidor,
- guardar por cámara `name`, `status`, `last_frame_at` y `last_inference_at`,
- exponerlos en el detalle del edge.

**A acordar antes de implementar:** la forma exacta de la respuesta y los
nombres de los campos. Los de acá salen de la spec del backend; confirmalos
antes de escribir la pantalla.

**Un detalle importante:** pedile que devuelva **instantes absolutos**
(`last_frame_at`), no "hace N segundos". El edge manda antigüedades porque su
reloj no es confiable, pero la API las convierte con el suyo al recibirlas. Si
la API te pasara el número relativo, envejecería entre que se guarda y se
muestra.
