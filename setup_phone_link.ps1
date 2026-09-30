param(
    [switch]$Install,
    [switch]$StartService,
    [switch]$Lan,
    [string]$HostIp
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$adbCandidates = @(
    (Join-Path $project 'tmp/android-sdk/platform-tools/adb.exe'),
    (Join-Path $project 'tmp/android-tools/platform-tools/adb.exe')
)
$adb = $adbCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $adb) {
    $command = Get-Command adb -ErrorAction SilentlyContinue
    if ($command) { $adb = $command.Source }
}
if (-not $adb) { throw 'ADB not found. Install Android SDK Platform-Tools first.' }

& $adb devices -l
if ($LASTEXITCODE -ne 0) { throw 'ADB device check failed.' }
$deviceState = (& $adb get-state 2>$null | Select-Object -First 1)
if ($deviceState -ne 'device') { throw 'No authorized Android device. Connect and unlock the phone for this one-time setup.' }

if ($Install) {
    $apk = Join-Path $project 'android-phone/app/build/outputs/apk/debug/app-debug.apk'
    if (-not (Test-Path -LiteralPath $apk)) { throw 'APK not found. Build android-phone first.' }
    & $adb install -r $apk
    if ($LASTEXITCODE -ne 0) { throw 'APK installation failed.' }
}

$tokenFile = Join-Path $project 'private/phone_link_token'
if (-not (Test-Path -LiteralPath $tokenFile)) {
    New-Item -ItemType Directory -Force (Split-Path -Parent $tokenFile) | Out-Null
    $bytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    [System.IO.File]::WriteAllText($tokenFile, $token)
}
$token = [System.IO.File]::ReadAllText($tokenFile).Trim()
$connectionArgs = @()
if ($Lan) {
    if (-not $HostIp) {
        $HostIp = Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'Wi-Fi' -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notmatch '^(127|169\.254)\.' } |
            Select-Object -First 1 -ExpandProperty IPAddress
    }
    if ($HostIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw 'Specify the computer Wi-Fi IPv4 address with -HostIp.' }
    $python = Join-Path $project '.venv/Scripts/python.exe'
    $fingerprint = (& $python -c 'from phone_link import tls_fingerprint; print(tls_fingerprint())').Trim()
    if ($LASTEXITCODE -ne 0 -or $fingerprint -notmatch '^[0-9a-f]{64}$') { throw 'Could not prepare the LAN TLS certificate.' }
    $connectionArgs = @('--es', 'link_host', $HostIp, '--es', 'link_cert_sha256', $fingerprint)
} else {
    & $adb reverse tcp:18765 tcp:18765
    if ($LASTEXITCODE -ne 0) { throw 'ADB reverse failed.' }
}
$startValue = if ($StartService) { 'true' } else { 'false' }
& $adb shell am start -n dev.specter.phone/.MainActivity --es link_token $token @connectionArgs --ez start_service $startValue
if ($LASTEXITCODE -ne 0) { throw 'Opening Specter Phone failed.' }
if ($Lan) {
    Write-Host "Phone is paired for encrypted Wi-Fi at $HostIp`:18766. USB may be disconnected after confirming a sample."
} else {
    Write-Host 'USB bridge is ready at phone localhost:18765.'
}
