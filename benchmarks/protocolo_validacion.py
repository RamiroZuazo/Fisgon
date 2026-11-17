"""
benchmarks/protocolo_validacion.py
Guía la sesión de validación funcional (bloque G) y registra cuándo estuvo el
arma a la vista, que es la verdad de referencia para el recall por evento.

Sirve una página que muestra cada ensayo en grande (distancia, ángulo,
iluminación), con tres fases y un pitido en cada cambio:
    PREPARATE  12 s  ubicarse en la posición, con el arma escondida
    MOSTRÁ      4 s  mostrar el arma hacia la cámara
    ESCONDÉ    8 s  esconder el arma (separa un ensayo del siguiente)

La página avisa a esta PC al empezar y al terminar cada fase MOSTRÁ, y la PC
lo anota con SU reloj (el mismo con el que el pipeline sella las detecciones),
de modo que no hay error de sincronización entre equipos.

Uso:
    python benchmarks/protocolo_validacion.py --salida benchmarks/resultados/g_ensayos.csv
    python benchmarks/protocolo_validacion.py --objeto cuchillo --salida benchmarks/resultados/g_ensayos_cuchillo.csv
    Abrir http://localhost:8766 en la PC (o http://<ip>:8766 en el celular).

Cada sesión va en un archivo nuevo: el analizador toma solo la primera sesión
completa de cada archivo.
"""

import argparse
import csv
import json
import socket
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PUERTO = 8766
DISTANCIAS = ("cerca (~1,5 m)", "media (~3 m)", "lejos (fondo)")
ANGULOS = ("de frente", "de costado")
# Cómo nombra la página al objeto: (con artículo, terminación de "escondid_").
OBJETOS = {"arma": ("el arma", "a"), "pistola": ("la pistola", "a"), "cuchillo": ("el cuchillo", "o")}


def armar_ensayos():
    ensayos = []
    for repeticion in (1, 2):
        for d in DISTANCIAS:
            for a in ANGULOS:
                ensayos.append({"distancia": d, "angulo": a, "luz": "encendida", "repeticion": repeticion})
    for d in DISTANCIAS:
        for a in ANGULOS:
            ensayos.append({"distancia": d, "angulo": a, "luz": "apagada", "repeticion": 1})
    for i, e in enumerate(ensayos, 1):
        e["n"] = i
    return ensayos


PAGINA = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Validación funcional</title>
<style>
  html,body{margin:0;height:100%;font-family:system-ui,sans-serif;background:#111;color:#fff}
  body{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
  #fase{font-size:13vw;font-weight:800;letter-spacing:.02em}
  #cuenta{font-size:9vw;font-weight:700;margin:1vh 0}
  #cond{font-size:4.5vw;line-height:1.3}
  #prog{font-size:2.5vw;color:#999;margin-top:2vh}
  button{font-size:3vw;padding:1.5vh 3vw;margin:2vh 1vw;border-radius:12px;border:0;cursor:pointer}
  .prep{background:#1d3557}.mostra{background:#1b7f3b}.esconde{background:#7a1f1f}.aviso{background:#8a6d00}
</style></head><body>
<div id="fase">Validación funcional</div>
<div id="cuenta"></div>
<div id="cond">__OBJETO_MAY__ escondid__A__ hasta que diga MOSTRÁ.<br>Pitido largo = mostrar · pitido corto = esconder.</div>
<div id="prog"></div>
<div><button id="btn" onclick="empezar()">Empezar</button><button onclick="pausar()">Pausa</button></div>
<script>
const ENSAYOS = __ENSAYOS__;
const T_PREP = 12, T_MOSTRA = 4, T_ESCONDE = 8, T_LUZ = 25;
let i = 0, pausado = false, corriendo = false, ctx = null;

function pitido(ms, f) {
  try {
    ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = f; o.connect(g); g.connect(ctx.destination);
    g.gain.value = 0.3; o.start(); setTimeout(() => o.stop(), ms);
  } catch (e) {}
}
function marcar(evento, e) {
  fetch('/marca', {method: 'POST', body: JSON.stringify({evento, ...e})});
}
function mostrar(fase, clase, cond) {
  document.body.className = clase;
  document.getElementById('fase').textContent = fase;
  document.getElementById('cond').innerHTML = cond;
}
async function esperar(seg) {
  for (let s = seg; s > 0; s--) {
    document.getElementById('cuenta').textContent = s;
    await new Promise(r => setTimeout(r, 1000));
    while (pausado) await new Promise(r => setTimeout(r, 200));
  }
  document.getElementById('cuenta').textContent = '';
}
function texto(e) {
  return `<b>Ensayo ${e.n} de ${ENSAYOS.length}</b><br>Distancia: ${e.distancia}<br>` +
         `Ángulo: ${e.angulo}<br>Luz: ${e.luz}`;
}
async function empezar() {
  if (corriendo) return;
  corriendo = true;
  document.getElementById('btn').style.display = 'none';
  pitido(150, 660);
  for (; i < ENSAYOS.length; i++) {
    const e = ENSAYOS[i];
    document.getElementById('prog').textContent = `${i} de ${ENSAYOS.length} completados`;
    if (i > 0 && e.luz !== ENSAYOS[i - 1].luz) {
      mostrar('APAGÁ LA LUZ', 'aviso', 'Dejá solo la luz de la pantalla o la del pasillo.');
      await esperar(T_LUZ);
    }
    mostrar('PREPARATE', 'prep', texto(e) + '<br><br>__OBJETO_MAY__ escondid__A__');
    await esperar(T_PREP);
    pitido(700, 880); marcar('inicio', e);
    mostrar('MOSTRÁ', 'mostra', texto(e));
    await esperar(T_MOSTRA);
    marcar('fin', e); pitido(200, 440);
    mostrar('ESCONDÉ', 'esconde', 'Escondé __OBJETO__');
    await esperar(T_ESCONDE);
  }
  mostrar('TERMINADO', 'prep', '¡Gracias! Ya podés prender la luz.');
  document.getElementById('prog').textContent = `${ENSAYOS.length} de ${ENSAYOS.length} completados`;
  marcar('sesion_terminada', {});
}
function pausar() {
  pausado = !pausado;
  document.getElementById('prog').textContent = pausado ? 'EN PAUSA (tocar Pausa para seguir)' : '';
}
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--salida", required=True)
    parser.add_argument("--objeto", choices=sorted(OBJETOS), default="arma")
    args = parser.parse_args()
    salida = Path(args.salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    ensayos = armar_ensayos()
    objeto, terminacion = OBJETOS[args.objeto]
    pagina = (PAGINA.replace("__ENSAYOS__", json.dumps(ensayos, ensure_ascii=False))
              .replace("__OBJETO_MAY__", objeto[0].upper() + objeto[1:])
              .replace("__OBJETO__", objeto).replace("__A__", terminacion)).encode("utf-8")
    campos = ["hora_utc", "hora_legible", "evento", "n", "distancia", "angulo", "luz", "repeticion"]
    if not salida.exists():
        with open(salida, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(campos)

    class Manejador(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(pagina)

        def do_POST(self):
            ahora = time.time()  # lo primero: la hora de la marca
            datos = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            fila = [f"{ahora:.3f}", datetime.fromtimestamp(ahora).strftime("%H:%M:%S.%f")[:-3],
                    datos.get("evento"), datos.get("n"), datos.get("distancia"), datos.get("angulo"),
                    datos.get("luz"), datos.get("repeticion")]
            with open(salida, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(fila)
            print(" | ".join(str(x) for x in fila[1:]))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.168.100.1", 80))
        ip = s.getsockname()[0]
    print(f"Protocolo: {len(ensayos)} ensayos. Abrir http://localhost:{PUERTO} o http://{ip}:{PUERTO}")
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador).serve_forever()


if __name__ == "__main__":
    main()
