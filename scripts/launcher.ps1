#requires -Version 5.1

<##
    Foreground Content Bot process launcher.
    Use the "Content Bot: Start" VS Code task to run backend and frontend together.
##>

param(
    [ValidateSet("backend", "frontend", "doctor", "setup-mediacrawler", "setup-subtitles")]
    [string]$Action = "backend",
    [ValidateSet("tiny", "base", "small", "medium", "large-v3")]
    [string]$SubtitleModel = "small",
    [switch]$DownloadSubtitleModel,
    [ValidateRange(1024, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1024, 65535)]
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot

function Test-LocalListener([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Initialize-BackendEnvironment {
    $python = "backend\.venv\Scripts\python.exe"
    $dependencyFile = "backend\pyproject.toml"
    $stampFile = "backend\.venv\.content-bot-dependencies"
    $dependencyHash = (Get-FileHash -LiteralPath $dependencyFile -Algorithm SHA256).Hash
    $installedHash = if (Test-Path -LiteralPath $stampFile) {
        (Get-Content -LiteralPath $stampFile -Raw).Trim()
    } else {
        ""
    }
    if (-not (Test-Path $python)) {
        Write-Host "Creating the backend virtual environment..."
        python -m venv backend\.venv
        if ($LASTEXITCODE -ne 0) { throw "Unable to create the backend environment." }
    }
    if ($installedHash -ne $dependencyHash) {
        Write-Host "Installing changed backend dependencies..."
        & $python -m pip install -e backend
        if ($LASTEXITCODE -ne 0) { throw "Unable to install backend dependencies." }
        Set-Content -LiteralPath $stampFile -Value $dependencyHash -Encoding ascii
    }
}

function Initialize-FrontendEnvironment {
    $dependencyFile = "frontend\package-lock.json"
    $stampFile = "frontend\node_modules\.content-bot-dependencies"
    $dependencyHash = (Get-FileHash -LiteralPath $dependencyFile -Algorithm SHA256).Hash
    $installedHash = if (Test-Path -LiteralPath $stampFile) {
        (Get-Content -LiteralPath $stampFile -Raw).Trim()
    } else {
        ""
    }
    if (-not (Test-Path "frontend\node_modules") -or $installedHash -ne $dependencyHash) {
        Write-Host "Installing changed frontend dependencies..."
        Push-Location frontend
        npm ci
        $installExitCode = $LASTEXITCODE
        Pop-Location
        if ($installExitCode -ne 0) { throw "Unable to install frontend dependencies." }
        Set-Content -LiteralPath $stampFile -Value $dependencyHash -Encoding ascii
    }
}

function Invoke-Backend {
    if (Test-LocalListener $BackendPort) {
        throw "Port $BackendPort is already in use. Stop the existing backend before starting this VS Code task."
    }
    Initialize-BackendEnvironment
    Write-Host "Starting Content Bot backend in the foreground at http://127.0.0.1:$BackendPort"
    & backend\.venv\Scripts\python.exe -u -m uvicorn app.main:app `
        --app-dir backend `
        --host 127.0.0.1 `
        --port $BackendPort `
        --reload `
        --reload-dir backend\app
    if ($LASTEXITCODE -ne 0) { throw "Backend stopped with code $LASTEXITCODE." }
}

function Invoke-Frontend {
    if (Test-LocalListener $FrontendPort) {
        throw "Port $FrontendPort is already in use. Stop the existing frontend before starting this VS Code task."
    }
    Initialize-FrontendEnvironment
    Write-Host "Starting Content Bot frontend in the foreground at http://127.0.0.1:$FrontendPort"
    Push-Location frontend
    npm run dev -- --host 127.0.0.1 --port $FrontendPort --strictPort
    $frontendExitCode = $LASTEXITCODE
    Pop-Location
    if ($frontendExitCode -ne 0) { throw "Frontend stopped with code $frontendExitCode." }
}

function Invoke-Doctor([switch]$Json, [switch]$Deep, [switch]$Save) {
    $checks = [System.Collections.Generic.List[object]]::new()

    function Add-Check([string]$Name, [string]$State, [string]$Detail, [bool]$Required = $false) {
        $checks.Add([pscustomobject]@{
            name = $Name
            state = $State
            detail = $Detail
            required = $Required
        })
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $pythonVersion = (& python --version 2>&1) -join " "
        $pythonMinor = [version]((& python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1) -join "")
        Add-Check "Python" $(if ($pythonMinor -ge [version]"3.11") { "pass" } else { "fail" }) "$pythonVersion; 3.11+ required." $true
    } else {
        Add-Check "Python" "fail" "Python 3.11+ is required." $true
    }

    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if ($nodeCommand) {
        $nodeVersion = ((& node --version 2>&1) -join " ").TrimStart("v")
        Add-Check "Node.js" $(if ([version]$nodeVersion -ge [version]"20.0") { "pass" } else { "fail" }) "v$nodeVersion; 20+ required." $true
    } else {
        Add-Check "Node.js" "fail" "Node.js 20+ is required." $true
    }

    Add-Check "Backend environment" $(if (Test-Path "backend\.venv\Scripts\python.exe") { "pass" } else { "setup" }) $(if (Test-Path "backend\.venv\Scripts\python.exe") { "backend/.venv is installed." } else { "Start Content Bot to create it." })
    Add-Check "Frontend packages" $(if (Test-Path "frontend\node_modules") { "pass" } else { "setup" }) $(if (Test-Path "frontend\node_modules") { "frontend/node_modules is installed." } else { "Start Content Bot to install them." })

    $mediaMain = Test-Path "vendor\mediacrawler\main.py"
    $mediaRuntime = Test-Path "vendor\mediacrawler\.venv\Scripts\python.exe"
    $mediaMarker = Test-Path "data\mediacrawler-ready"
    if ($mediaMain -and $mediaRuntime -and $mediaMarker) {
        Add-Check "MediaCrawler" "pass" "Submodule and browser runtime are ready."
    } elseif ($mediaMain) {
        Add-Check "MediaCrawler" "setup" "Run setup from this launcher."
    } else {
        Add-Check "MediaCrawler" "setup" "Run git submodule update --init --recursive."
    }

    if ($Deep -and $mediaRuntime) {
        $mediaPython = "vendor\mediacrawler\.venv\Scripts\python.exe"
        $probeScript = "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop(); print('ok')"
        $probeOutput = (& $mediaPython -c $probeScript 2>&1) -join " "
        if ($LASTEXITCODE -eq 0 -and $probeOutput -match "ok") {
            Add-Check "Chromium launch" "pass" "Bundled Chromium launched successfully."
        } else {
            Add-Check "Chromium launch" "fail" "Browser launch failed; run setup from this launcher." $true
        }
    } elseif ($Deep) {
        Add-Check "Chromium launch" "fail" "MediaCrawler runtime is missing." $true
    }

    $envPath = "backend\.env"
    $youtubeConfigured = $false
    if (Test-Path $envPath) {
        $youtubeConfigured = [bool](Select-String -Path $envPath -Pattern '^YOUTUBE_API_KEY\s*=\s*\S+' -Quiet)
    }
    Add-Check "YouTube key" $(if ($youtubeConfigured) { "pass" } else { "optional" }) $(if ($youtubeConfigured) { "YOUTUBE_API_KEY is present (value hidden)." } else { "Optional." })

    foreach ($port in 8000, 5173) {
        $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        Add-Check "Port $port" $(if ($listener) { "pass" } else { "idle" }) $(if ($listener) { "Listening locally (PID $($listener.OwningProcess))." } else { "Available." })
    }

    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 3
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/ready" -TimeoutSec 3
        Add-Check "API health" "pass" "Healthy and ready; $($health.sources) sources registered."
    } catch {
        Add-Check "API health" "fail" "API is not healthy or MongoDB is not ready; inspect the Backend task terminal." $true
    }

    if ($Save) {
        $diagnosticsRoot = Join-Path $taskRoot "data\diagnostics"
        New-Item -ItemType Directory -Path $diagnosticsRoot -Force | Out-Null
        $timestamp = [DateTimeOffset]::Now.ToString("yyyyMMdd-HHmmss-fff")
        $diagnosticsPath = Join-Path $diagnosticsRoot "doctor-$timestamp.json"
        [pscustomobject]@{
            generated_at = [DateTimeOffset]::Now.ToString("o")
            checks = @($checks)
            redaction_note = "Credential values, browser cookies and API tokens are never included."
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $diagnosticsPath -Encoding utf8
    }

    if ($Json) {
        $checks | ConvertTo-Json -Depth 3
    } else {
        $checks | Format-Table -AutoSize
        if ($Save) { Write-Host "Saved redacted diagnostics: $diagnosticsPath" }
    }

    $requiredFailure = $checks | Where-Object { $_.required -and $_.state -eq "fail" }
    if ($requiredFailure) { throw "One or more required checks failed." }
}

function Invoke-SetupMediaCrawler {
    $crawlerRoot = Join-Path $taskRoot "vendor\mediacrawler"
    $runtime = Join-Path $crawlerRoot ".venv\Scripts\python.exe"
    $readyMarker = Join-Path $taskRoot "data\mediacrawler-ready"

    if (-not (Test-Path (Join-Path $crawlerRoot "main.py"))) {
        git -C $taskRoot submodule update --init --recursive
        if ($LASTEXITCODE -ne 0) { throw "Unable to initialize MediaCrawler submodule." }
    }
    if (-not (Test-Path $runtime)) {
        python -m venv (Join-Path $crawlerRoot ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Unable to create MediaCrawler virtual environment." }
    }

    & $runtime -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Unable to upgrade MediaCrawler pip." }
    $dependencies = & $runtime -c "import pathlib,tomllib; p=pathlib.Path(r'$crawlerRoot')/'pyproject.toml'; print(chr(10).join(tomllib.loads(p.read_text(encoding='utf-8'))['project']['dependencies']))"
    if ($LASTEXITCODE -ne 0) { throw "Unable to read MediaCrawler dependencies." }
    & $runtime -m pip install $dependencies
    if ($LASTEXITCODE -ne 0) { throw "Unable to install MediaCrawler dependencies." }
    & $runtime -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Unable to install Playwright Chromium." }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $readyMarker) | Out-Null
    New-Item -ItemType File -Force -Path $readyMarker | Out-Null
    Write-Host "MediaCrawler is ready."
}

function Invoke-SetupSubtitles([string]$Model, [switch]$DownloadModel) {
    $runtime = Join-Path $taskRoot "backend\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $runtime)) {
        python -m venv (Join-Path $taskRoot "backend\.venv")
        if ($LASTEXITCODE -ne 0) { throw "Unable to create the backend virtual environment." }
    }

    & $runtime -m pip install -e "backend[alignment]"
    if ($LASTEXITCODE -ne 0) { throw "Unable to install subtitle alignment dependencies." }

    if ($DownloadModel) {
        $modelRoot = Join-Path $taskRoot "data\models\faster-whisper"
        & $runtime "backend\scripts\download_subtitle_model.py" --model $Model --cache-dir $modelRoot
        if ($LASTEXITCODE -ne 0) { throw "Unable to download the Faster Whisper model." }
        Write-Host "Faster Whisper model '$Model' is ready."
        Write-Host "Set CONTENT_BOT_ALIGNMENT_ENGINE=faster_whisper and CONTENT_BOT_ALIGNMENT_WHISPER_MODEL=$Model in backend\.env, then restart Content Bot."
    } else {
        Write-Host "Subtitle alignment dependencies are ready."
        Write-Host "Energy alignment remains the lightweight default."
        Write-Host "To enable word-level Faster Whisper alignment, rerun with -DownloadSubtitleModel."
    }
}

try {
    switch ($Action) {
        "backend" { Invoke-Backend }
        "frontend" { Invoke-Frontend }
        "doctor" { Invoke-Doctor }
        "setup-mediacrawler" { Invoke-SetupMediaCrawler }
        "setup-subtitles" { Invoke-SetupSubtitles -Model $SubtitleModel -DownloadModel:$DownloadSubtitleModel }
    }
} catch {
    Write-Error "Content Bot could not start: $($_.Exception.Message)"
    exit 1
}
