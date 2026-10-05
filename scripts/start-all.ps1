$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'service-state.ps1')
$python = Join-Path $root '.venv\Scripts\python.exe'
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$statePath = Join-Path $logs 'services.json'
$saved = @(Read-ServiceState $statePath)
$services = [ordered]@{ livekit = 7880; wssproxy = 7443; agent = 9090; wsvoice = 7444; frontend = 3000 }
foreach ($service in $services.Keys) {
    $entry = $saved | Where-Object { $_.name -eq $service } | Select-Object -First 1
    $proc = Find-ServiceLauncher $root $service $entry
    if ($proc) {
        $saved = @($saved | Where-Object { $_.name -ne $service }) + [pscustomobject]@{ name = $service; pid = $proc.ProcessId }
        ConvertTo-Json -InputObject @($saved) | Set-Content -LiteralPath $statePath
        Write-Host "$service already running (PID $($proc.ProcessId))"
        continue
    }
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $services[$service] -ErrorAction SilentlyContinue
    if ($listeners) { throw "Port $($services[$service]) occupied by PID $($listeners[0].OwningProcess)." }
    $arguments = '-u "' + (Join-Path $PSScriptRoot 'run-service.py') + '" ' + $service
    $p = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "$service.out.log") -RedirectStandardError (Join-Path $logs "$service.err.log")
    $saved = @($saved | Where-Object { $_.name -ne $service }) + [pscustomobject]@{ name = $service; pid = $p.Id }
    ConvertTo-Json -InputObject @($saved) | Set-Content -LiteralPath $statePath
    Write-Host "Starting $service (PID $($p.Id))"
}
$deadline = (Get-Date).AddSeconds(90)
do {
    $down = @($services.Keys | Where-Object { -not (Get-NetTCPConnection -State Listen -LocalPort $services[$_] -ErrorAction SilentlyContinue) })
    if (-not $down.Count) {
        try { $health = Invoke-WebRequest -Uri 'http://127.0.0.1:9090/healthz' -UseBasicParsing -TimeoutSec 2; if ($health.StatusCode -eq 200) { break } } catch { }
        $down = @('agent')
    }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
foreach ($service in $services.Keys) { Write-Host "$service : $($services[$service]) $(if ($down -contains $service) { 'DOWN' } else { 'UP' })" }
if ($down.Count) { throw "Services not ready: $($down -join ', '). Check $logs" }
Write-Host 'Open https://192.168.197.45:3000 on the LAN (or https://localhost:3000 on this PC).'
Write-Host 'Press Start call and wait for the agent to be ready.'
Write-Host 'For LAN use the computer hostname/IP; trust the local certificate on ports 3000, 7443 and 7444.'
