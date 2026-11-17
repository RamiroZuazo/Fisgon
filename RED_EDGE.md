# Red del edge: cámaras por cable, internet por WiFi

Guía de comandos para dejar el edge funcionando en un lugar sin cable de red
(por ejemplo, la facultad): las cámaras van por cable a un switch PoE junto con
el edge, y el edge sale a internet solo por WiFi.

```
 [Cámara Tiandy cam_3]──┐
                        ├── [Switch PoE] ──cable── [Edge] ))) WiFi ))) Internet → nube
 [Cámara Tiandy cam_4]──┘
       red de cámaras (cable, aislada)              red del lugar (WiFi, DHCP)
```

## Direcciones

| Equipo | Interfaz | IP | Máscara | Puerta de enlace |
|---|---|---|---|---|
| Edge | Ethernet (al switch) | 192.168.77.10 | 255.255.255.0 | **ninguna** |
| Edge | Wi-Fi | la da el DHCP del lugar | (automática) | (automática) |
| cam_4 (Tiandy) | cable | 192.168.77.31 | 255.255.255.0 | 192.168.77.10 |
| cam_3 (Tiandy 2) | cable | 192.168.77.67 | 255.255.255.0 | 192.168.77.10 |
| Notebook (para administrar) | Ethernet (al switch) | 192.168.77.50 | 255.255.255.0 | **ninguna** |

Red de la facultad (referencia): máscara 255.255.128.0, puerta de enlace
10.100.0.1, o sea 10.100.0.0 a 10.100.127.255. No choca con 192.168.77.x.

> El Ethernet del edge **no lleva puerta de enlace**: si la tuviera, Windows
> podría intentar salir a internet por el cable (donde solo hay cámaras) en vez
> de por el WiFi.

---

## 1. IP fija en el Ethernet del edge (una sola vez)

En el edge, **PowerShell como administrador** (clic derecho en Inicio →
*Windows PowerShell (Administrador)*; la línea tiene que decir
`PS C:\Windows\system32>`).

Apaga el DHCP en el Ethernet (en el switch no hay quien reparta IPs):
```powershell
Set-NetIPInterface -InterfaceAlias Ethernet -Dhcp Disabled
```

Le pone la IP fija 192.168.77.10, sin puerta de enlace:
```powershell
New-NetIPAddress -InterfaceAlias Ethernet -IPAddress 192.168.77.10 -PrefixLength 24
```

Borra los DNS del Ethernet (los DNS los da el WiFi):
```powershell
Set-DnsClientServerAddress -InterfaceAlias Ethernet -ResetServerAddresses
```

Alternativa con ventanas: `Win + R` → `ncpa.cpl` → clic derecho en
**Ethernet** → Propiedades → **TCP/IPv4** → "Usar la siguiente dirección IP":
192.168.77.10 / 255.255.255.0, puerta de enlace y DNS vacíos.

**No tocar el Wi-Fi**: tiene que quedar en automático (DHCP).

## 2. IP fija en las cámaras Tiandy (una sola vez)

En la página web de cada cámara → **Basic Set → Network**: destildar **DHCP**,
completar y **Save**. Al guardar la página se corta (la cámara ya cambió de IP).

| Campo | cam_4 | cam_3 |
|---|---|---|
| IPv4 Address | 192.168.77.31 | 192.168.77.67 |
| Subnet Mask | 255.255.255.0 | 255.255.255.0 |
| Gateway | 192.168.77.10 | 192.168.77.10 |

Después hay que actualizar las URLs `rtsp://...` de esas cámaras en
`cameras.yaml` (o desde el panel en `http://localhost:8000`).

## 3. IP fija en la notebook (para administrar el edge)

En la **notebook**: `Win + R` → `ncpa.cpl` → clic derecho en su **Ethernet** →
Propiedades → **TCP/IPv4**:
- Dirección IP: **192.168.77.50**
- Máscara: **255.255.255.0**
- Puerta de enlace y DNS: **vacíos**

Enchufar la notebook al switch. El WiFi de la notebook se deja como está.

## 4. Servidor SSH en el edge (una sola vez)

En el edge, **PowerShell como administrador**.

Instala el servidor OpenSSH que trae Windows (lo baja de Windows Update,
tarda uno o dos minutos):
```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
```

Hace que SSH arranque solo con la PC, y lo arranca ahora:
```powershell
Set-Service sshd -StartupType Automatic; Start-Service sshd
```

Limita SSH a la red de cámaras: solo acepta conexiones desde 192.168.77.x (lo
enchufado al switch). Desde el WiFi del lugar el edge ni responde por SSH:
```powershell
Set-NetFirewallRule -Name OpenSSH-Server-In-TCP -RemoteAddress 192.168.77.0/24
```

> Por qué la restricción: el usuario `fisgon` es administrador. Sin ella, el
> puerto SSH queda abierto en cualquier WiFi al que se conecte el edge (la red
> de la facultad admite ~32.000 equipos) y cualquiera podría probar contraseñas.

Opcional: permitir **ping** al edge, también solo desde la red de cámaras
(sirve para comprobar desde la notebook que el edge está prendido):
```powershell
New-NetFirewallRule -DisplayName "Fisgon - Ping (solo red de camaras)" -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -RemoteAddress 192.168.77.0/24 -Action Allow -Profile Any
```

## 5. Conectarse desde la notebook

Comprueba que el edge responde (necesita la regla de ping del paso 4):
```
ping 192.168.77.10
```

Entra al edge por SSH (pide la **contraseña** de `fisgon`, no el PIN; la
primera vez pregunta si confiás en el equipo: responder `yes`):
```
ssh fisgon@192.168.77.10
```

Para salir de la sesión SSH:
```
exit
```

## 6. Cambiar la red WiFi del edge (por SSH)

### Red nueva (con una sola clave, WPA2-Personal): usar el script

Solo la **primera vez** para cada red. Guarda la red con conexión automática,
se conecta y espera a que el edge tenga IP:
```
powershell -ExecutionPolicy Bypass -File C:\Users\fisgon\conectar_wifi.ps1 -Red "NOMBRE_DE_LA_RED" -Clave "LA_CLAVE"
```
Si sale bien muestra `[OK] Conectado a '...' con IP ...`.

> La clave queda en el historial de esa sesión SSH: cerrar la sesión al
> terminar. Mientras cambia de red el edge queda unos segundos sin internet;
> Fisgon guarda los eventos en `eventos.db` y los sube cuando vuelve.

### Red ya guardada: comandos de Windows

Muestra las redes que el edge ya tiene guardadas:
```
netsh wlan show profiles
```

Muestra las redes WiFi que el edge ve en este momento (con su señal):
```
netsh wlan show networks mode=bssid
```

Se pasa a una red ya guardada:
```
netsh wlan connect name="NOMBRE_DE_LA_RED"
```

Muestra a qué red está conectado ahora y con qué señal:
```
netsh wlan show interfaces
```

Muestra las IPs de cada interfaz (para ver la que le dio el WiFi):
```
ipconfig
```

Si hay varias redes guardadas al alcance, define cuál prefiere (1 = la
primera):
```
netsh wlan set profileorder name="NOMBRE_DE_LA_RED" interface="Wi-Fi" priority=1
```

Borra una red guardada:
```
netsh wlan delete profile name="NOMBRE_DE_LA_RED"
```

### Red con usuario y contraseña (WPA2-Enterprise, como la de la facultad)

El script **no** sirve para estas redes: Windows pide el usuario y la
contraseña en una ventana que por SSH no se ve, y `netsh` no puede cargarlos.
Hay que conectarse **una vez con monitor y teclado** en el edge (ícono de WiFi →
la red → usuario y contraseña → si aparece aviso de certificado, **Conectar** →
dejar tildado **Conectar automáticamente**). Desde ahí queda guardada y se
maneja con los comandos de arriba.

> Si cambia la contraseña de ese usuario, el edge pierde internet. Mejor pedir
> a sistemas un usuario propio para el dispositivo.

## 7. Comprobar que Fisgon sube a la nube

En el edge, muestra las últimas líneas del log (tiene que aparecer
`Pulso enviado` cada 5 minutos y las cámaras como `connected`):
```
powershell -Command "Get-Content C:\Users\fisgon\Fisgon\logs\edge.log -Tail 20"
```

Si no llega a la nube: el lugar tiene que dejar salir por HTTPS (puerto 443)
hacia el servidor de la API (Cloud Run). Si lo bloquea, pedirlo a sistemas.
