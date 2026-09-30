$ErrorActionPreference = 'Stop'
$avatarSettings = @{}
$envPath = Join-Path $PSScriptRoot '.env'
if (Test-Path -LiteralPath $envPath) {
    foreach ($line in Get-Content -LiteralPath $envPath -Encoding utf8) {
        if ($line -match '^\s*(SPECTER_[A-Z0-9_]+)\s*=(.*)$') {
            $avatarSettings[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
        }
    }
}
function Get-AvatarSetting([string]$name, [string]$fallback) {
    $processValue = [Environment]::GetEnvironmentVariable($name, 'Process')
    if ($null -ne $processValue) { return $processValue }
    if ($avatarSettings.ContainsKey($name)) { return $avatarSettings[$name] }
    return $fallback
}
$enabled = Get-AvatarSetting 'SPECTER_AVATAR_ENABLED' (Get-AvatarSetting 'SPECTER_DH_LIVE_ENABLED' 'true')
if ($enabled.Trim().ToLowerInvariant() -notin @('1', 'true', 'yes', 'on')) { return }
$backend = (Get-AvatarSetting 'SPECTER_AVATAR_BACKEND' 'dh_live').Trim().ToLowerInvariant()
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($backend -eq 'feathertalk') {
        # EN: Replace only the lab loop process so both pages share one GPU model.
        # 中文：仅替换实验循环服务进程，让两个页面共享同一份 GPU 模型。
        $labCompose = Join-Path $PSScriptRoot 'feathertalk_lab\compose.yaml'
        & docker compose -f $labCompose stop loop
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop the standalone FeatherTalk loop service' }
        & docker compose up -d --build --wait --wait-timeout 120 feathertalk
    } elseif ($backend -eq 'dh_live') {
        $model = (Get-AvatarSetting 'SPECTER_DH_LIVE_MODEL' 'mini').Trim().ToLowerInvariant()
        if ($model -eq 'full') { & docker compose up -d dh-live-full }
        elseif ($model -eq 'mini') { & docker compose up -d dh-live-inference dh-live-mini }
        else { throw 'SPECTER_DH_LIVE_MODEL must be mini or full' }
    } else {
        throw 'SPECTER_AVATAR_BACKEND must be dh_live or feathertalk'
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not start avatar backend: $backend" }
} finally {
    Pop-Location
}
