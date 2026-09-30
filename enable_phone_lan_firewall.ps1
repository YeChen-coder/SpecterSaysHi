param([string]$HostIp)

$ErrorActionPreference = 'Stop'
if (-not $HostIp) {
    $HostIp = Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'Wi-Fi' -ErrorAction Stop |
        Where-Object { $_.IPAddress -notmatch '^(127|169\.254)\.' } |
        Select-Object -First 1 -ExpandProperty IPAddress
}
if ($HostIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw 'A Wi-Fi IPv4 address is required.' }

$name = 'Specter Phone LAN 18766'
$existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
if ($existing) {
    $existing | Remove-NetFirewallRule -ErrorAction Stop
}
New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 18766 -LocalAddress $HostIp `
    -RemoteAddress LocalSubnet -Profile Private,Public -ErrorAction Stop | Out-Null
if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
    throw 'Windows did not create the Specter Phone firewall rule.'
}
Write-Host "Allowed Specter Phone LAN on $HostIp`:18766 from the local subnet only."
