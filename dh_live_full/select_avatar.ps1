param(
    [Parameter(Mandatory = $true)]
    [string]$Version
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$versionsRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'avatar_versions'))
$source = [System.IO.Path]::GetFullPath((Join-Path $versionsRoot $Version))
if (-not $source.StartsWith($versionsRoot + [System.IO.Path]::DirectorySeparatorChar,
                           [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Avatar version must be inside avatar_versions'
}
$files = @('circle.mp4', 'keypoint_rotate.pkl', 'avatar_config.json')
$config = Get-Content -LiteralPath (Join-Path $source 'avatar_config.json') -Raw | ConvertFrom-Json
if ($config.reference_file) {
    if ([System.IO.Path]::GetFileName($config.reference_file) -ne $config.reference_file) {
        throw 'reference_file must be a filename inside the avatar version'
    }
    $files += $config.reference_file
}
if ($config.teeth_file) {
    if ([System.IO.Path]::GetFileName($config.teeth_file) -ne $config.teeth_file) {
        throw 'teeth_file must be a filename inside the avatar version'
    }
    $files += $config.teeth_file
}
foreach ($field in @('idle_video','idle_mouth')) {
    $filename = $config.$field
    if ($filename) {
        if ([System.IO.Path]::GetFileName($filename) -ne $filename) {
            throw "$field must be a filename inside the avatar version"
        }
        $files += $filename
    }
}
foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $file))) {
        throw "Avatar version is incomplete: $Version/$file"
    }
}
Push-Location -LiteralPath $projectRoot
try {
    & docker compose stop dh-live-full
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop full DH_live before changing its assets' }
    foreach ($file in $files) {
        Copy-Item -LiteralPath (Join-Path $source $file) -Destination (Join-Path $PSScriptRoot "avatar/$file") -Force
    }
    & docker compose up -d --no-deps dh-live-full
    if ($LASTEXITCODE -ne 0) { throw 'Could not restart full DH_live' }
} finally {
    Pop-Location
}
Write-Host "Selected full DH_live avatar: $Version. Refresh http://127.0.0.1:18890/"
