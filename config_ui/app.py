"""
config_ui/app.py
Interfaz web local (localhost) para editar cameras.yaml sin tocar el archivo a mano.

Implementada solo con la librería estándar de Python (http.server), sin
FastAPI/Pydantic/Jinja2: en este equipo, Device Guard / Smart App Control
bloquea las extensiones compiladas de Pydantic (pydantic_core y también el
acelerador Cython de Pydantic v1), así que cualquier framework que dependa
de Pydantic no puede cargar. http.server evita el problema por completo al
no depender de ningún paquete de terceros con binarios compilados.

No corre junto al pipeline principal (main.py) — es una herramienta aparte,
para usar solo cuando hay que agregar/editar/borrar cámaras o ajustar
parámetros. Los cambios se aplican reiniciando main.py.

Requiere login. Las credenciales viven en config_ui/auth_store.json (usuario
+ hash PBKDF2 de la contraseña, nunca texto plano) y se bootstrapean con
admin/1234 la primera vez que corre el servidor si el archivo no existe.
Desde la sección "Perfil" de la interfaz se puede cambiar la contraseña.

Uso:
    python config_ui/app.py
    Abrir http://localhost:8000 en el navegador
"""

import binascii
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import yaml

CONFIG_PATH = Path(__file__).parent.parent / "cameras.yaml"
STATIC_DIR = Path(__file__).parent / "static"
AUTH_STORE_PATH = Path(__file__).parent / "auth_store.json"

DEFAULT_CONFIG = {
    "inference": {"model_path": "models/best.onnx", "confidence": 0.5, "iou": 0.45, "inference_fps": 5},
    "event_validation": {"min_consecutive_detections": 3, "window_seconds": 5},
    "api": {"base_url": ""},
    "cameras": [],
}

# Credenciales de arranque, solo se usan la primera vez (si auth_store.json
# no existe todavía) para crear el usuario inicial.
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "1234"
PBKDF2_ITERACIONES = 200_000

SESSION_COOKIE = "fisgon_session"
SESSION_TTL_SECONDS = 12 * 60 * 60  # 12 horas

# Sesiones activas en memoria: token -> timestamp de expiración.
# Se pierden al reiniciar el server (suficiente para una herramienta local).
_sesiones = {}


def cargar_config() -> dict:
    if not CONFIG_PATH.exists():
        return DEFAULT_CONFIG
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or DEFAULT_CONFIG


def guardar_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


def _resolver_fuente_test(rtsp_url: str):
    # Igual criterio que capture/camera_source.py: un valor numérico se
    # interpreta como índice de webcam local en vez de URL RTSP.
    if rtsp_url.strip().isdigit():
        return int(rtsp_url.strip())
    return rtsp_url


def _hash_password(password: str, salt: bytes = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    derivado = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERACIONES)
    return f"{binascii.hexlify(salt).decode()}:{binascii.hexlify(derivado).decode()}"


def _verificar_password(password: str, hash_guardado: str) -> bool:
    try:
        salt_hex, hash_hex = hash_guardado.split(":")
        salt = binascii.unhexlify(salt_hex)
    except (ValueError, binascii.Error):
        return False
    derivado = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERACIONES)
    return hmac.compare_digest(binascii.hexlify(derivado).decode(), hash_hex)


def _guardar_auth(auth: dict):
    with open(AUTH_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(auth, f)


def _cargar_auth() -> dict:
    if not AUTH_STORE_PATH.exists():
        auth = {"username": DEFAULT_ADMIN_USER, "password_hash": _hash_password(DEFAULT_ADMIN_PASSWORD)}
        _guardar_auth(auth)
        return auth
    with open(AUTH_STORE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _crear_sesion() -> str:
    token = secrets.token_urlsafe(32)
    _sesiones[token] = time.time() + SESSION_TTL_SECONDS
    return token


def _sesion_valida(token: str) -> bool:
    expira = _sesiones.get(token)
    if expira is None:
        return False
    if time.time() > expira:
        _sesiones.pop(token, None)
        return False
    return True


def render_login(error: str = None) -> str:
    error_html = f'<p class="login-error">{html.escape(error)}</p>' if error else ""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Fisgon - Acceder</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Chewy&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="/static/login.css">
</head>
<body>
    <header class="topbar">
        <img src="/static/eye/frame-00.png" alt="" class="topbar-logo-icon">
        <span class="auth-title-logo">FISGÓN</span>
    </header>

    <main class="login-main">
        <img id="eye-anim" class="eye-anim" src="/static/eye/frame-00.png" alt="Fisgon">

        <div class="login-card">
            <form method="post" action="/login">
                <label class="input-field">
                    <input type="text" name="username" placeholder="Nombre de usuario" autofocus required autocomplete="username">
                </label>
                <label class="input-field">
                    <input type="password" name="password" placeholder="Contraseña" required autocomplete="current-password">
                </label>
                {error_html}
                <button type="submit">Acceder</button>
            </form>
        </div>
    </main>

    <footer class="login-footer">
        © Fisgon — Proyecto Final Integrador. Todos los derechos reservados.
    </footer>

    <script>
        const totalFrames = 9;
        let idx = 0;
        const img = document.getElementById('eye-anim');
        setInterval(() => {{
            idx = (idx + 1) % totalFrames;
            img.src = `/static/eye/frame-0${{idx}}.png`;
        }}, 300);
    </script>
</body>
</html>"""


def render_index(config: dict, editar_id: str = None, error_camara: str = None, form_valores: dict = None) -> str:
    filas = ""
    for cam in config.get("cameras", []):
        cam_id_raw = str(cam.get("id", ""))
        cam_id = html.escape(cam_id_raw)

        if editar_id and cam_id_raw == editar_id:
            # Fila en modo edición inline: Nombre/RTSP/Activa se vuelven inputs
            name_value = html.escape(str(cam.get("name", "")))
            rtsp_value = html.escape(str(cam.get("rtsp_url", "")))
            active_checked = "checked" if cam.get("active") else ""
            filas += f"""
                <tr class="fila-edicion">
                    <td>{cam_id}</td>
                    <td colspan="4">
                        <form method="post" action="/camaras/editar" class="form-inline">
                            <input type="hidden" name="original_id" value="{cam_id}">
                            <input type="text" name="name" value="{name_value}" placeholder="Nombre" required>
                            <input type="text" name="rtsp_url" value="{rtsp_value}"
                                placeholder="RTSP o índice de webcam" required>
                            <label class="check-inline" title="Si está destildada, esta cámara no se usa en el pipeline">
                                <input type="checkbox" name="active" {active_checked}> Activa
                            </label>
                            <button type="submit">Guardar</button>
                            <a href="/" class="cancelar">Cancelar</a>
                        </form>
                    </td>
                </tr>"""
        else:
            cam_name = html.escape(str(cam.get("name", "")))
            cam_url = html.escape(str(cam.get("rtsp_url", "")))
            cam_url_js = cam_url.replace("'", "&#39;")
            filas += f"""
                <tr>
                    <td>{cam_id}</td>
                    <td>{cam_name}</td>
                    <td class="url">{cam_url}</td>
                    <td>{"Sí" if cam.get("active") else "No"}</td>
                    <td>
                        <button onclick="probarConexion('{cam_url_js}', this)">Probar conexión</button>
                        <a href="/?editar={cam_id}" class="editar">Editar</a>
                        <form method="post" action="/camaras/eliminar" style="display:inline">
                            <input type="hidden" name="id" value="{cam_id}">
                            <button type="submit" class="borrar">Eliminar</button>
                        </form>
                        <span class="resultado"></span>
                    </td>
                </tr>"""

    # Formulario de alta (siempre vacío salvo que un submit haya fallado, en
    # cuyo caso se repueblan los valores enviados para no perderlos).
    id_value = form_valores.get("id", "") if form_valores else ""
    name_value_alta = form_valores.get("name", "") if form_valores else ""
    rtsp_value_alta = form_valores.get("rtsp_url", "") if form_valores else ""
    active_checked_alta = (form_valores.get("active") == "on") if form_valores else True
    error_camara_html = f'<p class="form-error">{html.escape(error_camara)}</p>' if error_camara else ""

    inference = config.get("inference", {})
    ev = config.get("event_validation", {})
    api = config.get("api", {})

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Configuración - Fisgon</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <div class="topbar-dashboard">
        <h1>Configuración del sistema de detección</h1>
        <div class="topbar-links">
            <a href="/perfil" class="cerrar-sesion">Perfil</a>
            <a href="/logout" class="cerrar-sesion">Cerrar sesión</a>
        </div>
    </div>
    <p class="nota">Los cambios se aplican reiniciando <code>main.py</code>.</p>

    <section>
        <h2>Cámaras</h2>
        <table>
            <thead>
                <tr>
                    <th>ID</th><th>Nombre</th><th>RTSP</th><th>Activa</th><th>Acciones</th>
                </tr>
            </thead>
            <tbody>{filas}
            </tbody>
        </table>

        <h3>Agregar cámara</h3>
        <form method="post" action="/camaras/agregar">
            <label>ID (único): <input type="text" name="id" required placeholder="cam_1"
                value="{html.escape(id_value)}"></label>
            <label>Nombre: <input type="text" name="name" required placeholder="Entrada principal"
                value="{html.escape(name_value_alta)}"></label>
            <label>URL RTSP (o índice de webcam local, ej. 0): <input type="text" name="rtsp_url" required
                placeholder="rtsp://usuario:password@192.168.1.22:554/Streaming/Channels/101" size="60"
                value="{html.escape(rtsp_value_alta)}"></label>
            <label class="check-inline">
                <input type="checkbox" name="active" {"checked" if active_checked_alta else ""}> Activa
                <span class="ayuda">(si está destildada, no se usa en el pipeline)</span>
            </label>
            {error_camara_html}
            <button type="submit">Guardar cámara</button>
        </form>
    </section>

    <section>
        <h2>Ajustes de inferencia y eventos</h2>
        <form method="post" action="/ajustes/guardar">
            <label>Confianza mínima:
                <input type="number" step="0.01" name="confidence" value="{inference.get("confidence", 0.5)}"></label>
            <label>IOU:
                <input type="number" step="0.01" name="iou" value="{inference.get("iou", 0.45)}"></label>
            <label>FPS de inferencia por cámara:
                <input type="number" name="inference_fps" value="{inference.get("inference_fps", 5)}"></label>
            <label>Detecciones consecutivas para confirmar evento:
                <input type="number" name="min_consecutive_detections" value="{ev.get("min_consecutive_detections", 3)}"></label>
            <label>Ventana de tiempo (segundos):
                <input type="number" name="window_seconds" value="{ev.get("window_seconds", 5)}"></label>
            <label>URL base de la API en la nube:
                <input type="text" name="api_base_url" value="{html.escape(str(api.get("base_url", "")))}" size="50"></label>
            <button type="submit">Guardar ajustes</button>
        </form>
    </section>

    <script>
        async function probarConexion(url, boton) {{
            const span = boton.parentElement.querySelector('.resultado');
            span.textContent = "Probando...";
            const body = new URLSearchParams({{ rtsp_url: url }});
            const resp = await fetch("/camaras/probar", {{
                method: "POST",
                headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
                body: body,
            }});
            const data = await resp.json();
            span.textContent = data.mensaje;
            span.style.color = data.ok ? "green" : "red";
        }}
    </script>
</body>
</html>"""


def render_perfil(mensaje: str = None, error: str = None) -> str:
    mensaje_html = f'<p class="form-ok">{html.escape(mensaje)}</p>' if mensaje else ""
    error_html = f'<p class="form-error">{html.escape(error)}</p>' if error else ""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Perfil - Fisgon</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <div class="topbar-dashboard">
        <h1>Perfil</h1>
        <a href="/" class="cerrar-sesion">Volver</a>
    </div>

    <section>
        <h2>Cambiar contraseña</h2>
        <form method="post" action="/perfil/cambiar_contrasena">
            <label>Contraseña actual: <input type="password" name="actual" required autocomplete="current-password"></label>
            <label>Contraseña nueva: <input type="password" name="nueva" required autocomplete="new-password"></label>
            <label>Confirmar contraseña nueva: <input type="password" name="confirmar" required autocomplete="new-password"></label>
            {mensaje_html}
            {error_html}
            <button type="submit">Cambiar contraseña</button>
        </form>
    </section>
</body>
</html>"""


class ConfigUIHandler(BaseHTTPRequestHandler):
    server_version = "FisgonConfigUI/1.0"

    def log_message(self, format, *args):
        print(f"[config_ui] {self.address_string()} - {format % args}")

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = parse_qs(body)
        return {k: v[0] for k, v in parsed.items()}

    def _cookies(self) -> SimpleCookie:
        c = SimpleCookie()
        header = self.headers.get("Cookie")
        if header:
            c.load(header)
        return c

    def _autenticado(self) -> bool:
        cookies = self._cookies()
        if SESSION_COOKIE not in cookies:
            return False
        return _sesion_valida(cookies[SESSION_COOKIE].value)

    def _send_html(self, body: str, status: int = 200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj: dict, status: int = 200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, cookie_header: str = None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie_header:
            self.send_header("Set-Cookie", cookie_header)
        self.end_headers()

    def _servir_static(self, ruta: str):
        rel = ruta[len("/static/"):]
        destino = (STATIC_DIR / rel).resolve()
        if not destino.is_relative_to(STATIC_DIR.resolve()):
            self.send_response(403)
            self.end_headers()
            return
        content_type = "text/css; charset=utf-8" if destino.suffix == ".css" else \
            "image/png" if destino.suffix == ".png" else "application/octet-stream"
        self._send_file(destino, content_type)

    def do_GET(self):
        ruta = urlparse(self.path).path

        if ruta.startswith("/static/"):
            self._servir_static(ruta)
            return

        if ruta == "/login":
            if self._autenticado():
                self._redirect("/")
            else:
                self._send_html(render_login())
            return

        if ruta == "/logout":
            cookies = self._cookies()
            if SESSION_COOKIE in cookies:
                _sesiones.pop(cookies[SESSION_COOKIE].value, None)
            self._redirect("/login", cookie_header=f"{SESSION_COOKIE}=; Path=/; Max-Age=0")
            return

        if not self._autenticado():
            self._redirect("/login")
            return

        if ruta == "/":
            query = parse_qs(urlparse(self.path).query)
            editar_id = query.get("editar", [None])[0]
            self._send_html(render_index(cargar_config(), editar_id=editar_id))
        elif ruta == "/perfil":
            self._send_html(render_perfil())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        ruta = urlparse(self.path).path
        form = self._read_form()

        if ruta == "/login":
            self._procesar_login(form)
            return

        if not self._autenticado():
            if ruta == "/camaras/probar":
                self._send_json({"ok": False, "mensaje": "Sesión expirada, volvé a iniciar sesión."}, status=401)
            else:
                self._redirect("/login")
            return

        if ruta == "/camaras/agregar":
            error = self._agregar_camara(form)
            if error:
                self._send_html(render_index(cargar_config(), error_camara=error, form_valores=form), status=400)
            else:
                self._redirect("/")
        elif ruta == "/camaras/editar":
            error = self._editar_camara(form)
            if error:
                self._send_html(render_index(cargar_config(), error_camara=error), status=400)
            else:
                self._redirect("/")
        elif ruta == "/camaras/eliminar":
            self._eliminar_camara(form)
            self._redirect("/")
        elif ruta == "/ajustes/guardar":
            self._guardar_ajustes(form)
            self._redirect("/")
        elif ruta == "/camaras/probar":
            self._probar_conexion(form)
        elif ruta == "/perfil/cambiar_contrasena":
            error = self._cambiar_contrasena(form)
            if error:
                self._send_html(render_perfil(error=error), status=400)
            else:
                self._send_html(render_perfil(mensaje="Contraseña actualizada correctamente."))
        else:
            self.send_response(404)
            self.end_headers()

    def _procesar_login(self, form: dict):
        usuario = form.get("username", "")
        contrasena = form.get("password", "")

        auth = _cargar_auth()
        credenciales_ok = usuario == auth.get("username") and _verificar_password(contrasena, auth.get("password_hash", ""))
        if credenciales_ok:
            token = _crear_sesion()
            cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}"
            self._redirect("/", cookie_header=cookie)
        else:
            self._send_html(render_login(error="Usuario o contraseña incorrectos"), status=401)

    def _cambiar_contrasena(self, form: dict):
        """Devuelve None si se cambió OK, o un mensaje de error si no."""
        auth = _cargar_auth()
        actual = form.get("actual", "")
        nueva = form.get("nueva", "")
        confirmar = form.get("confirmar", "")

        if not _verificar_password(actual, auth.get("password_hash", "")):
            return "La contraseña actual no es correcta."
        if len(nueva) < 4:
            return "La contraseña nueva debe tener al menos 4 caracteres."
        if nueva != confirmar:
            return "La confirmación no coincide con la contraseña nueva."

        auth["password_hash"] = _hash_password(nueva)
        _guardar_auth(auth)
        return None

    def _agregar_camara(self, form: dict):
        """Da de alta una cámara nueva. Devuelve None si salió bien, o un
        mensaje de error (ej. ID duplicado) si no."""
        config = cargar_config()
        config.setdefault("cameras", [])
        cam_id = form.get("id", "").strip()
        active = form.get("active") == "on"  # checkbox: "on" si tildado, ausente si no
        datos = {"name": form.get("name", "").strip(), "rtsp_url": form.get("rtsp_url", "").strip(), "active": active}

        if not cam_id:
            return "El ID de la cámara no puede estar vacío."
        if any(str(c.get("id")) == cam_id for c in config["cameras"]):
            return f'Ya existe una cámara con el ID "{cam_id}". Elegí otro ID o usá "Editar" en la tabla.'

        config["cameras"].append({"id": cam_id, **datos})
        guardar_config(config)
        return None

    def _editar_camara(self, form: dict):
        """Actualiza nombre/RTSP/activa de una cámara existente (el ID no
        cambia). Devuelve None si salió bien, o un mensaje de error si no."""
        config = cargar_config()
        original_id = form.get("original_id", "").strip()
        active = form.get("active") == "on"
        datos = {"name": form.get("name", "").strip(), "rtsp_url": form.get("rtsp_url", "").strip(), "active": active}

        existentes = [c for c in config.get("cameras", []) if str(c.get("id")) == original_id]
        if not existentes:
            return "La cámara que intentás editar ya no existe."

        existentes[0].update(datos)
        guardar_config(config)
        return None

    def _eliminar_camara(self, form: dict):
        config = cargar_config()
        cam_id = form.get("id", "")
        config["cameras"] = [c for c in config.get("cameras", []) if c["id"] != cam_id]
        guardar_config(config)

    def _guardar_ajustes(self, form: dict):
        config = cargar_config()
        config.setdefault("inference", {})
        config.setdefault("event_validation", {})
        config["inference"]["confidence"] = float(form.get("confidence", 0.5))
        config["inference"]["iou"] = float(form.get("iou", 0.45))
        config["inference"]["inference_fps"] = int(form.get("inference_fps", 5))
        config["event_validation"]["min_consecutive_detections"] = int(form.get("min_consecutive_detections", 3))
        config["event_validation"]["window_seconds"] = int(form.get("window_seconds", 5))
        config.setdefault("api", {})["base_url"] = form.get("api_base_url", "")
        guardar_config(config)

    def _probar_conexion(self, form: dict):
        """Intenta abrir el RTSP (o índice de webcam) brevemente y devuelve si conectó o no."""
        fuente = _resolver_fuente_test(form.get("rtsp_url", ""))
        cap = cv2.VideoCapture(fuente)
        conectado = cap.isOpened()
        frame_ok = False
        if conectado:
            frame_ok, _ = cap.read()
        cap.release()

        if conectado and frame_ok:
            self._send_json({"ok": True, "mensaje": "Conexión exitosa, se recibió video."})
        elif conectado:
            self._send_json({"ok": False, "mensaje": "Conectó pero no llegó video. Revisá el canal/ruta."})
        else:
            self._send_json({"ok": False, "mensaje": "No se pudo conectar. Revisá IP, puerto y credenciales."})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8000), ConfigUIHandler)
    print("[config_ui] Corriendo en http://localhost:8000  (Ctrl+C para detener)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
