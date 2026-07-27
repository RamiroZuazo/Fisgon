# instalar.ps1
# Arma el entorno para correr el edge de Fisgon en una maquina nueva:
# crea un entorno virtual de Python, instala las dependencias exactas
# (ver requirements.txt), crea las carpetas que hacen falta, y al final
# corre una verificacion REAL de que la GPU (DirectML) este activa antes
# de decir que termino - no alcanza con que pip diga "instalado con exito".
#
# NOTA: este archivo se escribe sin tildes ni guiones largos a proposito.
# PowerShell 5.1 (el que trae Windows por defecto) puede romper el
# parseo de caracteres especiales si el archivo se copia/transfiere sin
# preservar bien la codificacion UTF-8 - mejor evitarlo del todo.
#
# Uso (desde una consola de PowerShell, parada en la carpeta del proyecto):
#   .\instalar.ps1
#
# Ver INSTALACION.md para la guia completa paso a paso (incluye que hacer
# si algo de esto falla).

$ErrorActionPreference = "Stop"
$raiz = $PSScriptRoot
Set-Location $raiz

function Escribir-Titulo($texto) {
    Write-Host ""
    Write-Host "=== $texto ===" -ForegroundColor Cyan
}

function Escribir-Error($texto) {
    Write-Host "[ERROR] $texto" -ForegroundColor Red
}

function Escribir-Ok($texto) {
    Write-Host "[OK] $texto" -ForegroundColor Green
}

# --- 1. Verificar Python 3.12 ---
Escribir-Titulo "Verificando Python"

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Escribir-Error "No se encontro 'python' en el PATH. Instala Python 3.12 (python.org) y volve a correr este script."
    exit 1
}

$version = & python --version 2>&1
if ($version -notmatch "3\.12") {
    Escribir-Error "Se encontro $version, pero este proyecto esta fijado a Python 3.12 (por la rueda de onnxruntime-directml==1.24.4, compilada especificamente para 3.12). Instala Python 3.12 y volve a correr."
    exit 1
}
Escribir-Ok "Python 3.12 encontrado: $version"

# --- 2. Crear entorno virtual ---
Escribir-Titulo "Creando entorno virtual (venv)"

if (Test-Path "$raiz\venv") {
    Write-Host "Ya existe venv - se reutiliza. Si queres un entorno limpio, borra esa carpeta primero y volve a correr."
} else {
    & python -m venv "$raiz\venv"
    Escribir-Ok "Entorno virtual creado en venv"
}

$venvPython = "$raiz\venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Escribir-Error "No se encontro $venvPython - la creacion del entorno virtual fallo."
    exit 1
}

# --- 3. Instalar dependencias ---
Escribir-Titulo "Instalando dependencias (requirements.txt)"

& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r "$raiz\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Escribir-Error "pip install fallo. Revisa el mensaje de arriba."
    exit 1
}
Escribir-Ok "Dependencias instaladas"

# --- 4. Crear carpetas necesarias ---
Escribir-Titulo "Creando carpetas"

foreach ($carpeta in @("logs", "models")) {
    if (-not (Test-Path "$raiz\$carpeta")) {
        New-Item -ItemType Directory -Path "$raiz\$carpeta" | Out-Null
        Write-Host "Creada: $carpeta"
    }
}
Escribir-Ok "Carpetas listas"

# --- 5. Chequear archivos de configuracion ---
Escribir-Titulo "Verificando configuracion"

if (-not (Test-Path "$raiz\.env")) {
    Write-Host "[AVISO] No existe .env - sin el, upload_worker no va a poder subir eventos a la nube." -ForegroundColor Yellow
    Write-Host "        Crea uno con:"
    Write-Host "          FISGON_API_URL=https://tu-api"
    Write-Host "          FISGON_EDGE_TOKEN=tu-token"
    Write-Host "        (ver INSTALACION.md, seccion 'Configurar credenciales')"
} else {
    Escribir-Ok ".env encontrado"
}

if (-not (Test-Path "$raiz\cameras.yaml")) {
    Escribir-Error "No existe cameras.yaml - hace falta para arrancar. Copia el de otra instalacion o arma uno nuevo (ver INSTALACION.md)."
    exit 1
} else {
    Escribir-Ok "cameras.yaml encontrado"
}

if (-not (Test-Path "$raiz\models\best.onnx")) {
    Write-Host "[AVISO] No existe models\best.onnx - copialo desde la maquina de desarrollo antes de arrancar el pipeline." -ForegroundColor Yellow
} else {
    Escribir-Ok "Modelo encontrado (models\best.onnx)"
}

# --- 6. Verificar que la GPU (DirectML) este realmente activa ---
Escribir-Titulo "Verificando GPU (DirectML) - puede tardar unos segundos"

if (Test-Path "$raiz\models\best.onnx") {
    # Se escribe a un archivo temporal en vez de pasar el script inline con
    # -c: un script de varias lineas pasado como argumento de linea de
    # comandos a traves de PowerShell puede llegar mal escapado a python.exe
    # (paso por esto: fallaba diciendo "ultralytics no esta instalado" aun
    # estando instalado, porque el argumento le llegaba mal a python).
    $scriptVerificacion = @"
import sys
sys.path.insert(0, r'$raiz')
try:
    from inference.model_loader import cargar_modelo
    modelo = cargar_modelo(r'$raiz\models\best.onnx')
    modelo.predict(__import__('numpy').zeros((640,640,3), dtype='uint8'), verbose=False)
    session = modelo.predictor.model.session
    proveedores = session.get_providers()
    if 'DmlExecutionProvider' in proveedores:
        print('DIRECTML_OK')
    else:
        print('DIRECTML_FALTA:' + str(proveedores))
except Exception as e:
    print('ERROR:' + str(e))
"@
    $archivoTemp = "$raiz\_verificar_gpu_temp.py"
    $scriptVerificacion | Out-File -FilePath $archivoTemp -Encoding utf8

    # Un fallo aca es una ADVERTENCIA (GPU mal configurada), no un motivo
    # para abortar todo el instalador — se baja $ErrorActionPreference
    # temporalmente para que un exit code distinto de 0 de python.exe no
    # corte el script entero (que arriba esta seteado a "Stop").
    $prefAnterior = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $resultado = & $venvPython $archivoTemp 2>&1 | Select-Object -Last 5
    $ErrorActionPreference = $prefAnterior
    $ultimaLinea = ($resultado | Select-Object -Last 1).ToString()

    Remove-Item $archivoTemp -ErrorAction SilentlyContinue

    if ($ultimaLinea -like "DIRECTML_OK*") {
        Escribir-Ok "GPU AMD (DirectML) activa y funcionando"
    } elseif ($ultimaLinea -like "DIRECTML_FALTA*") {
        Write-Host "[AVISO] El modelo carga pero corre por CPU, no por GPU. Proveedores: $ultimaLinea" -ForegroundColor Yellow
        Write-Host "        Revisa que los drivers de AMD esten actualizados en esta maquina."
    } else {
        Write-Host "[AVISO] No se pudo verificar la GPU automaticamente:" -ForegroundColor Yellow
        Write-Host "        $resultado"
    }
} else {
    Write-Host "Se salteo (no hay modelo todavia)."
}

# --- Resumen final ---
Escribir-Titulo "Instalacion terminada"
Write-Host ""
Write-Host "Para correr el sistema:"
Write-Host "  venv\Scripts\python.exe run.py --config cameras.yaml"
Write-Host ""
Write-Host "Para la interfaz de configuracion (si no la levantaste con run.py):"
Write-Host "  venv\Scripts\python.exe config_ui\app.py   ->  http://localhost:8000"
Write-Host ""
Write-Host "Ver INSTALACION.md para dejarlo arrancando solo al prender la PC."
