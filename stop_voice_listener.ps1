$ErrorActionPreference = 'Stop'
$taskName = 'SpecterSaysHiVoiceListener'
$scriptPath = Join-Path $PSScriptRoot 'specter.py'
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$scriptPattern = [regex]::Escape($scriptPath)
$pythonPattern = [regex]::Escape($venvPython)
Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq 'python.exe' -and
        $_.CommandLine -match $scriptPattern -and
        $_.CommandLine -match $pythonPattern
    } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
