param([ValidateSet('cpu', 'cuda')][string]$Device = 'cpu')
$ErrorActionPreference = 'Stop'
$voiceRoot = Join-Path (Split-Path $PSScriptRoot -Parent) 'runtimes/voiceover'
$voiceEnvironment = if ($Device -eq 'cuda') { '.venv-gpu' } else { '.venv' }
$voicePython = Join-Path $voiceRoot "$voiceEnvironment/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $voicePython)) {
    python -m venv (Join-Path $voiceRoot $voiceEnvironment)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create voice runtime.' }
}
& $voicePython -m pip install -r (Join-Path $voiceRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Cannot install voice runtime.' }
if ($Device -eq 'cuda') {
    & $voicePython -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
    if ($LASTEXITCODE -ne 0) { throw 'Cannot install CUDA dependencies.' }
    & $voicePython -m pip install -r (Join-Path $voiceRoot 'requirements-gpu.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot install transformers.' }
}
$env:PYTHONUTF8 = '1'
& $voicePython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Voice runtime has incompatible dependencies. Resolve the package errors above.' }
if ($Device -eq 'cuda') {
    & $voicePython (Join-Path $voiceRoot 'check_gpu.py')
    if ($LASTEXITCODE -ne 0) { throw 'CUDA execution check failed.' }
}
& $voicePython (Join-Path $voiceRoot 'worker.py') prepare $Device
if ($LASTEXITCODE -ne 0) { throw 'Voice model preparation failed. See output above.' }
