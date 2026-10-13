"""
benchmarks/reloj_pc.py
Sirve una página con la hora de ESTA PC al milisegundo, para mostrarla en un
celular frente a una cámara y medir la demora de la cámara: cuánto pasa entre
que algo ocurre frente al lente y que el pipeline captura ese frame.

Por qué la hora de la PC y no la de un reloj cualquiera: el pipeline sella cada
frame con el reloj de esta PC (ver logs/latencia_eventos.csv,
captura_confirmante_utc). Si el celular muestra ese mismo reloj, la diferencia
entre la hora que se ve en el snapshot y la hora de captura es solo la demora
de la cámara y del flujo RTSP, sin errores de sincronización entre relojes.

La página se sincroniza con la PC varias veces por segundo y corrige la mitad
del tiempo de ida y vuelta de cada consulta (en la red local, unos pocos ms);
muestra ese error en pantalla.

Uso:
    python benchmarks/reloj_pc.py            (puerto 8765)
    En el celular, conectado a la misma red: http://<ip-de-la-pc>:8765
"""

import json
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUERTO = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

PAGINA = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reloj de la PC</title>
<style>
  html,body{margin:0;height:100%;background:#000;color:#fff;font-family:monospace}
  body{display:flex;flex-direction:column;align-items:center;justify-content:center}
  #hora{font-size:15vw;font-weight:bold;letter-spacing:.02em}
  #ms{font-size:22vw;font-weight:bold;color:#ff0}
  #info{font-size:4vw;color:#888;margin-top:2vh}
</style></head><body>
<div id="hora">--:--:--</div><div id="ms">.---</div><div id="info">sincronizando...</div>
<script>
let desfase = 0, mejorRtt = Infinity;
async function sincronizar() {
  try {
    const t0 = performance.now();
    const r = await fetch('/hora', {cache: 'no-store'});
    const t1 = performance.now();
    const {ahora} = await r.json();
    const rtt = t1 - t0;
    // Se queda con la consulta más rápida de las últimas: es la de menor error.
    if (rtt <= mejorRtt * 1.5) {
      desfase = ahora * 1000 + rtt / 2 - (performance.timeOrigin + t1);
      mejorRtt = Math.min(mejorRtt, rtt);
      document.getElementById('info').textContent =
        'hora de la PC · error ±' + Math.ceil(rtt / 2) + ' ms';
    }
  } catch (e) {
    document.getElementById('info').textContent = 'sin conexión con la PC';
  }
}
function pintar() {
  const d = new Date(performance.timeOrigin + performance.now() + desfase);
  const dos = n => String(n).padStart(2, '0');
  document.getElementById('hora').textContent =
    dos(d.getHours()) + ':' + dos(d.getMinutes()) + ':' + dos(d.getSeconds());
  document.getElementById('ms').textContent = '.' + String(d.getMilliseconds()).padStart(3, '0');
  requestAnimationFrame(pintar);
}
sincronizar(); setInterval(sincronizar, 300); setInterval(() => { mejorRtt *= 1.2; }, 5000);
pintar();
</script></body></html>"""


class Manejador(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/hora"):
            cuerpo = json.dumps({"ahora": time.time()}).encode()
            tipo = "application/json"
        else:
            cuerpo = PAGINA.encode("utf-8")
            tipo = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *args):
        pass  # una consulta cada 300 ms llenaría la consola


def ip_local() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.168.100.1", 80))  # no envía nada: solo elige la interfaz de la red local
        return s.getsockname()[0]


if __name__ == "__main__":
    print(f"Reloj de la PC en http://{ip_local()}:{PUERTO}  (Ctrl+C para cortar)")
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador).serve_forever()
