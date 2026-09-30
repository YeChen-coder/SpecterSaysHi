$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$featherCommit = 'ace1227ec0a367a983101bfc17d7e5fcf2bfb63f'
if (!(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'upstream'))) {
    git clone https://github.com/anliyuan/FeatherTalk.git upstream
    if ($LASTEXITCODE -ne 0) { throw 'FeatherTalk clone failed' }
    git -C upstream checkout $featherCommit
    if ($LASTEXITCODE -ne 0) { throw 'FeatherTalk checkout failed' }
}
$featherHead = git -C upstream rev-parse HEAD
if ($featherHead.Trim() -ne $featherCommit) { throw "Expected upstream commit $featherCommit, found $featherHead" }
New-Item -ItemType Directory -Force data,output,checkpoints | Out-Null
docker compose build worker
if ($LASTEXITCODE -ne 0) { throw 'FeatherTalk image build failed' }
