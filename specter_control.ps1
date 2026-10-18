param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'Stop')]
    [string]$Action,
    [switch]$DryRun,
    [switch]$ForceRecreate
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$composeFile = Join-Path $projectRoot 'compose.yaml'
$composeProject = 'specter-says-hi'
$baseServices = @('mediamtx', 'mosquitto', 'frigate')
$cameraTaskName = 'SpecterSaysHiCameraBridge'
$voiceTaskName = 'SpecterSaysHiVoiceListener'
$cameraScript = Join-Path $projectRoot 'camera_bridge.ps1'
$voiceScript = Join-Path $projectRoot 'voice_listener.ps1'
$agentScript = Join-Path $projectRoot 'specter.py'
$avatarScript = Join-Path $projectRoot 'start_avatar.ps1'
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$cameraPublishUrl = 'rtsp://127.0.0.1:18554/desk_camera'

# EN: Never use a broad Docker or process stop here. EBOBot runs in a separate
# Compose project and must remain online when Specter Says Hi is stopped.
# 中文：这里只操作本项目的 Compose 名称、计划任务和进程；EBOBot 独立运行。
function Test-ContainsPath([string]$Text, [string]$Path) {
    return ($Text.IndexOf($Path, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
}

function Get-ProjectTask([string]$Name, [string]$ExpectedScript) {
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        throw "Required Specter task is missing: $Name"
    }
    $matchingAction = @($task.Actions | Where-Object {
        Test-ContainsPath ([string]$_.Arguments) $ExpectedScript
    })
    if ($matchingAction.Count -ne 1) {
        throw "Task $Name no longer points to $ExpectedScript; refusing to control it"
    }
    return $task
}

function Invoke-ProjectCompose([string]$Subcommand, [string[]]$ExtraArgs = @()) {
    Write-Host "Docker Compose [$composeProject]: $Subcommand -d $($ExtraArgs -join ' ')"
    if ($DryRun) { return }
    & $docker.Source compose -p $composeProject --project-directory $projectRoot -f $composeFile $Subcommand -d @ExtraArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $Subcommand failed with exit code $LASTEXITCODE"
    }
}

function Stop-ProjectProcesses {
    # EN: Task Scheduler can leave descendants behind. Match both this exact
    # project path and the project-only RTSP publishing endpoint before killing.
    # 中文：计划任务停止后可能留下子进程，只清理命令行明确属于本项目的进程。
    $processes = @(Get-CimInstance Win32_Process | Where-Object {
        $line = [string]$_.CommandLine
        if ($_.Name -eq 'python.exe') {
            (Test-ContainsPath $line $venvPython) -and
            (Test-ContainsPath $line $agentScript)
        } elseif ($_.Name -in @('pwsh.exe', 'powershell.exe')) {
            (Test-ContainsPath $line $cameraScript) -or
            (Test-ContainsPath $line $voiceScript)
        } elseif ($_.Name -eq 'ffmpeg.exe') {
            Test-ContainsPath $line $cameraPublishUrl
        } else {
            $false
        }
    })
    foreach ($process in $processes) {
        Write-Host "Stopping Specter process: $($process.Name) PID $($process.ProcessId)"
        if (-not $DryRun) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

if (-not (Test-Path -LiteralPath $composeFile)) {
    throw "Specter Compose file is missing: $composeFile"
}
$docker = Get-Command docker -ErrorAction Stop
$cameraTask = Get-ProjectTask $cameraTaskName $cameraScript
$voiceTask = Get-ProjectTask $voiceTaskName $voiceScript

if ($Action -eq 'Start') {
    if (-not (Test-Path -LiteralPath $avatarScript)) {
        throw "Specter avatar startup script is missing: $avatarScript"
    }
    # EN: Compose starts only the named Specter project. Enable the two
    # logon-triggered tasks again so the system stays on after the next login.
    # 中文：先启动本项目容器，再启用并启动摄像头与语音计划任务。
    if ($ForceRecreate) {
        # Reload the Python prompt as well as the containers. Task Scheduler can
        # leave child Python/FFmpeg processes running after a task is stopped.
        foreach ($task in @($voiceTask, $cameraTask)) {
            Write-Host "Stopping $($task.TaskName) before recreation"
            if (-not $DryRun) {
                Stop-ScheduledTask -TaskName $task.TaskName -ErrorAction SilentlyContinue
            }
        }
        Stop-ProjectProcesses
        Invoke-ProjectCompose 'up' (@('--force-recreate') + $baseServices)
    } else {
        Invoke-ProjectCompose 'up' $baseServices
    }
    # Apply the selected backend/character, including profiled FeatherTalk,
    # before allowing camera or voice tasks to publish audio.
    Write-Host 'Starting the selected avatar via start_avatar.ps1'
    if (-not $DryRun) {
        & $avatarScript
    }
    foreach ($task in @($cameraTask, $voiceTask)) {
        Write-Host "Enabling and starting $($task.TaskName)"
        if (-not $DryRun) {
            Enable-ScheduledTask -TaskName $task.TaskName | Out-Null
            if ((Get-ScheduledTask -TaskName $task.TaskName).State -ne 'Running') {
                Start-ScheduledTask -TaskName $task.TaskName
            }
        }
    }
} else {
    # EN: Disable before stopping; otherwise a later Windows login could
    # silently reactivate the camera and microphone.
    # 中文：先禁用登录自启，再停止任务和残留进程，最后停止本项目容器。
    foreach ($task in @($voiceTask, $cameraTask)) {
        Write-Host "Disabling and stopping $($task.TaskName)"
        if (-not $DryRun) {
            Disable-ScheduledTask -TaskName $task.TaskName | Out-Null
            Stop-ScheduledTask -TaskName $task.TaskName -ErrorAction SilentlyContinue
        }
    }
    Stop-ProjectProcesses
    Write-Host "Docker Compose [$composeProject]: stop"
    if (-not $DryRun) {
        & $docker.Source compose -p $composeProject --project-directory $projectRoot -f $composeFile --profile '*' stop
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose stop failed with exit code $LASTEXITCODE"
        }
    }
}

Write-Host "Specter Says Hi $Action completed. EBOBot was not changed."
