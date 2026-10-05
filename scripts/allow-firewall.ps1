# Run once as Administrator to open Local Voice AI ports on all network profiles.
$ErrorActionPreference = 'Stop'

$pairs = @(
  @{ n = 'Local Voice AI - Next HTTPS 3000 (TCP)'; p = 'TCP'; port = 3000 },
  @{ n = 'Local Voice AI - WSS proxy 7443 (TCP)'; p = 'TCP'; port = 7443 },
  @{ n = 'Local Voice AI - Voice WS 7444 (TCP)'; p = 'TCP'; port = 7444 },
  @{ n = 'Local Voice AI - LiveKit signal 7880 (TCP)'; p = 'TCP'; port = 7880 },
  @{ n = 'Local Voice AI - LiveKit media 7881 (TCP)'; p = 'TCP'; port = 7881 },
  @{ n = 'Local Voice AI - LiveKit TURN relay 7881 (UDP)'; p = 'UDP'; port = 7881 },
  @{ n = 'Local Voice AI - LiveKit media 7882 (UDP)'; p = 'UDP'; port = 7882 },
  @{ n = 'Local Voice AI - LiveKit TURN TLS 443 (TCP)'; p = 'TCP'; port = 443 }
)

foreach ($x in $pairs) {
  $exists = Get-NetFirewallRule -DisplayName $x.n -ErrorAction SilentlyContinue
  if (-not $exists) {
    New-NetFirewallRule -DisplayName $x.n -Direction Inbound -Action Allow `
      -Profile Any -Protocol $x.p -LocalPort $x.port -ErrorAction Stop | Out-Null
  }
}

Set-Content -LiteralPath (Join-Path $PSScriptRoot '..\logs\firewall-rules-ok.txt') `
  -Value "Firewall rules verified at $(Get-Date -Format o)"

Write-Host 'Local Voice AI firewall rules are ready.'
