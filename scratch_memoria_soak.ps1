$pid_objetivo = 24168
$muestras = 25
$intervalo_seg = 60
$archivo = "D:\PFI\Fisgon\logs\memoria_soak.csv"
"timestamp,rss_mb" | Out-File -FilePath $archivo -Encoding utf8

for ($i = 0; $i -lt $muestras; $i++) {
    try {
        $p = Get-Process -Id $pid_objetivo -ErrorAction Stop
        $rss_mb = [Math]::Round($p.WorkingSet64 / 1MB, 1)
        $linea = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),$rss_mb"
        Add-Content -Path $archivo -Value $linea
    } catch {
        Add-Content -Path $archivo -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),PROCESO_NO_ENCONTRADO"
    }
    Start-Sleep -Seconds $intervalo_seg
}
