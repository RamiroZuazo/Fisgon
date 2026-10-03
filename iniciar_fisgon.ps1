# iniciar_fisgon.ps1
# Lo corre la tarea programada "Fisgon" al iniciar sesion (ver INSTALACION.md,
# paso 8): levanta run.py y, cuando el panel responde, abre
# http://localhost:8000 en el navegador predeterminado.
#
# Se queda esperando a run.py a proposito: asi la tarea figura "En ejecucion"
# mientras Fisgon este arriba, y si run.py termina con error el Programador de
# Tareas la reintenta (capa extra sobre el watchdog propio de run.py).
#
# Sin tildes a proposito (ver nota en instalar.ps1).

$raiz = $PSScriptRoot
Set-Location $raiz

$url = "http://localhost:8000"
$puerto = 8000
$esperaMaximaSegundos = 180

function Panel-Responde {
    $cliente = New-Object System.Net.Sockets.TcpClient
    try {
        $intento = $cliente.BeginConnect("127.0.0.1", $puerto, $null, $null)
        return ($intento.AsyncWaitHandle.WaitOne(1000) -and $cliente.Connected)
    } catch {
        return $false
    } finally {
        $cliente.Close()
    }
}

# Si ya hay un Fisgon corriendo (por ejemplo, lanzado a mano), no se levanta
# un segundo: dos pipelines en paralelo duplicarian eventos. Solo se abre la pagina.
if (Panel-Responde) {
    Start-Process $url
    exit 0
}

$proceso = Start-Process -FilePath "$raiz\venv\Scripts\python.exe" `
    -ArgumentList "run.py", "--config", "cameras.yaml" `
    -WorkingDirectory $raiz -NoNewWindow -PassThru

$limite = (Get-Date).AddSeconds($esperaMaximaSegundos)
while ((Get-Date) -lt $limite -and -not $proceso.HasExited) {
    if (Panel-Responde) {
        Start-Process $url
        break
    }
    Start-Sleep -Seconds 2
}

$proceso.WaitForExit()
exit $proceso.ExitCode
