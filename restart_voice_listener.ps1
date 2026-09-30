$ErrorActionPreference = 'Stop'
$taskName = 'SpecterSaysHiVoiceListener'
$scriptPath = (Join-Path $PSScriptRoot 'specter.py').ToLowerInvariant()

# EN: Apply the selected avatar service and its .env inference settings first.
# 中文：先应用 .env 选择的数字人服务及推理设置，再重启语音监听器。
& (Join-Path $PSScriptRoot 'start_avatar.ps1')

Stop-ScheduledTask -TaskName $taskName
$oldProcesses = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($scriptPath) })
foreach ($oldProcess in $oldProcesses) {
    Stop-Process -Id $oldProcess.ProcessId -Force -ErrorAction SilentlyContinue
}
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Ready') { break }
    Start-Sleep -Milliseconds 250
}
if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Ready') {
    throw "Voice listener task did not stop cleanly: $taskName"
}
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 2
if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Running') {
    throw "Voice listener task did not start: $taskName"
}
Write-Host "Specter voice listener restarted; stopped $($oldProcesses.Count) old Python process(es)."
