param(
    [string]$CameraName = 'Streaming Camera',
    [string]$Ffmpeg = 'C:\Program Files\kdenlive\bin\ffmpeg.exe',
    [int]$Width = 1280,
    [int]$Height = 720,
    [int]$Fps = 15
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Ffmpeg)) {
    throw "FFmpeg not found: $Ffmpeg"
}

$createdNew = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\SpecterSaysHiCameraBridge', [ref]$createdNew)
if (-not $createdNew) {
    Write-Host 'Camera bridge is already running in this Windows session.'
    $mutex.Dispose()
    exit 0
}

# Frigate runs in Docker Desktop's Linux VM, so capture the Windows USB camera
# on Windows and publish H.264 through the local MediaMTX RTSP listener.
try {
    while ($true) {
        Write-Host "Publishing '$CameraName' to rtsp://127.0.0.1:18554/desk_camera"
        & $Ffmpeg -hide_banner -loglevel warning `
            -f dshow -video_size "${Width}x${Height}" -framerate $Fps -vcodec mjpeg `
            -i "video=$CameraName" `
            -an -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p `
            -g ($Fps * 2) -b:v 1800k `
            -rtsp_transport tcp -f rtsp 'rtsp://127.0.0.1:18554/desk_camera'
        Write-Warning 'Camera capture stopped. Retrying in 5 seconds; check FFmpeg device name and camera access.'
        Start-Sleep -Seconds 5
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
