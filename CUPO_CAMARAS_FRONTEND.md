# Cupo de cámaras por edge — trabajo del panel de administración

Spec para el frontend web de administración. Autocontenido: no hace falta
conocer el código del edge ni de la API para trabajar con esto.

---

## 1. Contexto

Fisgon es un sistema de detección de armas. Cada cliente tiene un **edge**: una
PC instalada en su local que corre el pipeline (cámaras → modelo → alertas) y
sube los eventos a la API en la nube.

Cuando se da de alta un cliente, desde este panel se le crean **credenciales**:
un token que identifica a ese edge contra la API.

**Lo que falta:** cada cliente tiene contratada una cantidad máxima de cámaras,
y hoy eso no se registra en ningún lado. El edge puede levantar 20 cámaras y la
nube no se entera.

**La decisión tomada:** el cupo se guarda junto a las credenciales del edge, del
lado del backend. El edge lo consulta con su token al arrancar. Por eso este
panel es el lugar donde ese número se define y se cambia.

Que viva acá y no en la máquina del cliente tiene un motivo concreto: **cambiar
el plan de un cliente no puede requerir un acceso remoto a su PC.** Se cambia
desde acá y el edge lo toma en su próximo arranque.

---

## 2. Qué hay que hacer

### 2.1 Alta de credenciales — campo nuevo obligatorio

En el formulario que hoy crea las credenciales de un edge, sumar:

| Campo | Control | Reglas |
|---|---|---|
| **Cantidad de cámaras** | entero | Obligatorio. Mínimo 1. Sin valor por defecto precargado. |

**Que sea obligatorio y sin default importa.** Si el campo se puede saltear o
viene con un 4 precargado, en algún momento va a haber clientes con un cupo que
nadie decidió y nadie va a saber si es el plan real o un alta apurada.

Etiqueta sugerida y texto de ayuda:

> **Cantidad de cámaras**
> Cuántas cámaras puede tener activas este edge, según el plan contratado.

### 2.2 Edición — sin rotar el token

Desde la pantalla de administración del edge se tiene que poder **modificar el
cupo sin recrear las credenciales**. Subir a un cliente de 4 a 8 cámaras no
puede implicar reinstalarle el sistema ni cambiarle el token.

También hay que **mostrar el valor actual** en el listado o el detalle de cada
edge: sin eso, para saber el cupo de un cliente hay que abrir a editarlo.

### 2.3 Avisar cuando se baja el cupo

Bajar el número tiene una consecuencia real del otro lado: **el edge va a dejar
de arrancar cámaras**. Si un cliente tiene 6 activas y se le baja el cupo a 4,
dos dejan de vigilar.

Antes de confirmar una reducción, pedir confirmación explícita:

> **Vas a bajar el cupo de 6 a 4 cámaras.**
> El edge va a dejar de levantar 2 de las cámaras que hoy están activas. El
> operador del local va a poder elegir cuáles conservar desde su interfaz.
> [ Cancelar ] [ Bajar el cupo ]

Subir el cupo no necesita confirmación.

### 2.4 Cuándo toma efecto

El edge consulta su cupo **al arrancar el pipeline**, no en tiempo real. En la
práctica eso pasa al encender el sistema y cada vez que el operador guarda un
cambio de configuración en su interfaz local.

Conviene dejarlo dicho en la pantalla para que no parezca que el cambio no
funcionó:

> Los cambios de cupo se aplican en el próximo arranque del edge.

---

## 3. Qué **no** tiene que hacer este panel

- **No valida el cupo contra las cámaras reales.** El panel define el número; el
  control de que se cumpla es de la API, que rechaza los eventos de cámaras
  fuera de cupo. No hace falta consultar el estado del edge para dejar guardar.
- **No configura las cámaras.** Las URLs RTSP, los nombres y qué cámara está
  activa se cargan en la interfaz local del edge, en el local del cliente. Acá
  solo se define *cuántas* puede tener.

---

## 4. Lo que hay que acordar con backend

Este trabajo depende de que la API exponga el campo. Hay un documento paralelo
con el detalle de ese lado (`CUPO_CAMARAS_BACKEND.md`), donde ya está pedido:

- que el endpoint de creación **acepte y exija** el cupo,
- que exista una forma de **modificarlo** sin rotar el token,
- que el listado y el detalle lo **devuelvan**.

**A acordar antes de implementar:** el nombre exacto del campo. En la spec del
backend figura como `max_cameras`; confirmalo antes de escribir el formulario
para no terminar mapeando nombres distintos de cada lado.

---

## 5. Detalle útil, si el backend lo puede exponer

Mostrar en el listado de edges **cuántas cámaras tiene activas cada uno contra
su cupo** — algo como `3 / 4` — hace que se vea de un vistazo qué clientes están
al tope y son candidatos a subir de plan.

Requiere que la API devuelva también las cámaras activas reportadas por cada
edge, así que **es una mejora, no un bloqueante**. Vale la pena preguntarlo,
pero el alta y la edición del cupo se pueden entregar sin esto.
