param(
    [string]$Venv = (Join-Path $PSScriptRoot '.venv'),
    [string]$Python = '',           # base Python 3.12 interpreter; default: uv-managed 3.12 or the py launcher
    [string]$Uv = '',               # path to uv.exe; default: uv on PATH, else pip
    [switch]$SkipModel,             # do not download the Whisper model
    [switch]$SkipCudaLibs,          # do not install pip nvidia-cublas/cudnn (when CUDA 12 + cuDNN 9 DLLs are already available)
    [switch]$VerifyOnly
)
# Creates the Python environment, installs pinned packages and downloads the
# faster-whisper large-v3-turbo model into .\models. Nothing is installed system-wide.
$ErrorActionPreference = 'Stop'
$python = Join-Path $Venv 'Scripts\python.exe'
if (-not $Uv) { $cmd = Get-Command uv -ErrorAction SilentlyContinue; if ($cmd) { $Uv = $cmd.Source } }

if (-not $VerifyOnly) {
    if (-not (Test-Path -LiteralPath $python)) {
        if ($Uv) {
            $pyArg = if ($Python) { $Python } else { '3.12' }
            & $Uv venv --python $pyArg $Venv
        } else {
            if ($Python) { & $Python -m venv $Venv } else { & py -3.12 -m venv $Venv }
        }
        if ($LASTEXITCODE -ne 0) { throw 'Creating the Python environment failed.' }
    }
    $req = @('-r', (Join-Path $PSScriptRoot 'requirements.txt'))
    if (-not $SkipCudaLibs) { $req += @('-r', (Join-Path $PSScriptRoot 'requirements-cuda.txt')) }
    if ($Uv) { & $Uv pip install --python $python @req } else { & $python -m pip install @req }
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed.' }

    if (-not $SkipModel) {
        $target = Join-Path $PSScriptRoot 'models\faster-whisper-large-v3-turbo'
        if (-not (Test-Path -LiteralPath (Join-Path $target 'model.bin'))) {
            Write-Host "Downloading faster-whisper large-v3-turbo (~1.6 GB) to $target"
            & $python -c "import sys; from huggingface_hub import snapshot_download; snapshot_download('dropbox-dash/faster-whisper-large-v3-turbo', local_dir=sys.argv[1])" $target
            if ($LASTEXITCODE -ne 0) { throw 'Model download failed.' }
        }
    }
}
& $python (Join-Path $PSScriptRoot 'teams_caption\selfcheck.py')
if ($LASTEXITCODE -ne 0) { throw 'Self-check failed; see the [FAIL] lines above.' }
Write-Host 'Ready. Start a local translation server (see README), then double-click the launcher .cmd.'
