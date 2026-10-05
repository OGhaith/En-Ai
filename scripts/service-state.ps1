# Shared by both launchers. Compatible with Windows PowerShell 5.1 and PowerShell 7.
function Read-ServiceState {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
        # PS 5.1 emits a JSON array as ONE pipeline object. Assign first, then
        # enumerate it explicitly; otherwise every service gets the entire PID list.
        $parsed = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        foreach ($entry in $parsed) {
            if ($entry.name -and $entry.pid -is [ValueType]) { $entry }
        }
    } catch {
        Write-Warning 'Service state could not be read; existing project launchers will be rediscovered.'
    }
}

function Test-ServiceLauncher {
    param($Process, [string]$Root, [string]$Service)
    if (-not $Process -or -not $Process.CommandLine) { return $false }
    $script = Join-Path $Root 'scripts\run-service.py'
    $pattern = [regex]::Escape($script) + '"?\s+' + [regex]::Escape($Service) + '\s*$'
    return $Process.CommandLine -imatch $pattern
}

function Find-ServiceLauncher {
    param([string]$Root, [string]$Service, $SavedEntry)
    if ($SavedEntry) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$SavedEntry.pid)" -ErrorAction SilentlyContinue
        if (Test-ServiceLauncher $process $Root $Service) { return $process }
    }
    # Recover the parent launcher if the state file was lost or contains stale IDs.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { Test-ServiceLauncher $_ $Root $Service } |
        Sort-Object CreationDate |
        Select-Object -First 1
}
