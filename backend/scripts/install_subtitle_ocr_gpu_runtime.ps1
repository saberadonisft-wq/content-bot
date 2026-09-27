param(
    [string]$Python = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe")
)

$BackendRoot = Split-Path -Parent $PSScriptRoot
$ProfileRoot = Join-Path $BackendRoot "runtimes\ocr_gpu"
$ProfilePath = Join-Path $ProfileRoot "site-packages"
$InstallPath = Join-Path $ProfileRoot "site-packages.installing"
$Requirements = Join-Path $ProfileRoot "requirements.txt"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Backend Python was not found: $Python"
}
if (Test-Path -LiteralPath $ProfilePath) {
    $ExistingItems = @(Get-ChildItem -LiteralPath $ProfilePath -Force)
    if ($ExistingItems.Count -gt 0) {
        throw "OCR GPU runtime already exists at $ProfilePath. Install to a new empty path after stopping the backend."
    }
    Remove-Item -LiteralPath $ProfilePath
}
if (Test-Path -LiteralPath $InstallPath) {
    $StagingItems = @(Get-ChildItem -LiteralPath $InstallPath -Force)
    if ($StagingItems.Count -gt 0) {
        throw "Staging directory already has files at $InstallPath. Inspect it before retrying."
    }
    Remove-Item -LiteralPath $InstallPath
}

$BaseSmokePath = Join-Path $ProfileRoot "base-preflight-$([guid]::NewGuid().ToString('N')).py"
$BaseSmoke = @'
from packaging.version import Version
import flatbuffers
import google.protobuf
import numpy
import packaging
if Version(numpy.__version__) < Version('1.21.6'):
    raise SystemExit(f'Backend NumPy is too old for ONNX Runtime GPU: {numpy.__version__}')
print('Backend supplies flatbuffers, NumPy, packaging, and protobuf.')
'@
Set-Content -LiteralPath $BaseSmokePath -Value $BaseSmoke -Encoding Ascii
try {
    & $Python $BaseSmokePath
    if ($LASTEXITCODE -ne 0) {
        throw "Backend is missing shared ONNX Runtime GPU dependencies (exit $LASTEXITCODE)."
    }
}
finally {
    Remove-Item -LiteralPath $BaseSmokePath -Force -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
& $Python -m pip install --only-binary=:all: --ignore-installed --no-deps --target $InstallPath -r $Requirements
if ($LASTEXITCODE -ne 0) {
    throw "OCR GPU dependency installation failed (exit $LASTEXITCODE). Staging was kept for inspection."
}

$SmokePath = Join-Path $ProfileRoot "gpu-preflight-$([guid]::NewGuid().ToString('N')).py"
$Smoke = @'
import json
import os
import sys
from pathlib import Path
import numpy
profile = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(profile))
handles = []
for directory in (profile / 'nvidia').glob('*/bin'):
    if hasattr(os, 'add_dll_directory'):
        handles.append(os.add_dll_directory(str(directory)))
import onnxruntime as ort
preload = getattr(ort, 'preload_dlls', None)
if preload:
    preload()
if ort.__version__ != '1.26.0':
    raise SystemExit(f'Unexpected ONNX Runtime version: {ort.__version__}')
if 'CUDAExecutionProvider' not in ort.get_available_providers():
    raise SystemExit(f'CUDAExecutionProvider is missing: {ort.get_available_providers()}')
if not Path(ort.__file__).resolve().is_relative_to(profile):
    raise SystemExit(f'ONNX Runtime was imported outside its profile: {ort.__file__}')
if Path(numpy.__file__).resolve().is_relative_to(profile):
    raise SystemExit('GPU profile unexpectedly shadows the backend NumPy module.')
sys.path.insert(0, str(Path(sys.argv[2]).resolve()))
from app.services.subtitle_ocr import _create_ocr_engine, _extract_crop_text
engine, stage_providers = _create_ocr_engine(use_cuda=True)
if any(not providers or providers[0] != 'CUDAExecutionProvider' for providers in stage_providers.values()):
    raise SystemExit(f'OCR stage provider mismatch: {stage_providers}')
_extract_crop_text(engine, numpy.zeros((96, 320, 3), dtype=numpy.uint8))
print(json.dumps({'version': ort.__version__, 'providers': ort.get_available_providers(), 'stage_providers': stage_providers, 'module': ort.__file__, 'numpy': numpy.__version__, 'inference_smoke': True}))
'@
Set-Content -LiteralPath $SmokePath -Value $Smoke -Encoding Ascii
try {
    & $Python $SmokePath $InstallPath $BackendRoot
    if ($LASTEXITCODE -ne 0) {
        throw "OCR GPU runtime preflight failed (exit $LASTEXITCODE). Staging was kept for inspection."
    }
}
finally {
    Remove-Item -LiteralPath $SmokePath -Force -ErrorAction SilentlyContinue
}

$RuntimeManifest = @{
    schema_version = 1
    onnxruntime_gpu = "1.26.0"
    requirements_sha256 = (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash.ToLowerInvariant()
}
$ManifestPath = Join-Path $InstallPath "runtime-manifest.json"
$ManifestJson = $RuntimeManifest | ConvertTo-Json -Compress
Set-Content -LiteralPath $ManifestPath -Value $ManifestJson -Encoding Ascii

Move-Item -LiteralPath $InstallPath -Destination $ProfilePath
Write-Host "Installed isolated OCR GPU runtime at $ProfilePath"
