$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$script = Join-Path $projectRoot 'specter.py'
$logDir = Join-Path $projectRoot 'logs'
$runnerLog = Join-Path $logDir 'voice_runner.log'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'config.local.env'))) {
    throw 'config.local.env is missing'
}
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

$createdNew = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\SpecterSaysHiVoiceListener', [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

try {
    while ($true) {
        try {
            Push-Location -LiteralPath $projectRoot
            try {
                $env:PYTHONIOENCODING = 'utf-8'
                & $python $script *> $null
                $exitCode = $LASTEXITCODE
            } finally {
                Pop-Location
            }
            if ($exitCode -eq 42) {
                Start-Sleep -Seconds 60
                continue
            }
            Add-Content -LiteralPath $runnerLog -Value "$(Get-Date -Format o) specter.py exited with code $exitCode; restarting in 5 seconds"
        } catch {
            Add-Content -LiteralPath $runnerLog -Value "$(Get-Date -Format o) voice listener launch failed: $_; retrying in 5 seconds"
        }
        Start-Sleep -Seconds 5
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
