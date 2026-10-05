$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'service-state.ps1')
$statePath = Join-Path $root 'logs\services.json'
$saved = @(Read-ServiceState $statePath)
foreach ($service in @('frontend', 'agent', 'wsvoice', 'wssproxy', 'livekit')) {
    $entry = $saved | Where-Object { $_.name -eq $service } | Select-Object -First 1
    $proc = Find-ServiceLauncher $root $service $entry
    if ($proc) {
        Write-Host "Stopping $service (PID $($proc.ProcessId))"
        taskkill.exe /PID $proc.ProcessId /T /F | Out-Null
    }
}
'[]' | Set-Content -LiteralPath $statePath
