#requires -Version 5.1

<##
    Foreground Content Bot process launcher.
    Use "Content Bot: Start" to open the desktop app, or "Content Bot: Web Services" for browser development.
##>

param(
    [ValidateSet("auth", "backend", "frontend", "desktop", "doctor", "setup-mediacrawler", "setup-subtitles", "update", "rollback")]
    [string]$Action = "backend",
    [ValidateSet("tiny", "base", "small", "medium", "large-v3")]
    [string]$SubtitleModel = "small",
    [switch]$DownloadSubtitleModel,
    [ValidateRange(1024, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1024, 65535)]
    [int]$FrontendPort = 5173,
    [ValidateRange(1024, 65535)]
    [int]$AuthPort = 8080,
    [string]$AuthServerUrl = "",
    [switch]$SkipUpdateCheck,
    [switch]$ForceUpdate
)

$ErrorActionPreference = "Stop"
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot

function Get-CurrentAppVersion {
    $versionFile = Join-Path $taskRoot "backend\app\version.py"
    if (Test-Path -LiteralPath $versionFile) {
        $content = Get-Content -LiteralPath $versionFile -Raw
        if ($content -match 'APP_VERSION\s*=\s*["'']([^"'']+)["'']') {
            return $matches[1].Trim()
        }
    }
    return "0.1.0"
}

function Get-AuthServerEndpoint {
    if ($AuthServerUrl) { return $AuthServerUrl.TrimEnd('/') }
    
    $envFile = Join-Path $taskRoot "backend\.env"
    if (Test-Path -LiteralPath $envFile) {
        $authUrlLine = Select-String -Path $envFile -Pattern '^CONTENT_BOT_AUTH_SERVER_URL\s*=\s*(\S+)' | Select-Object -First 1
        if ($authUrlLine -and $authUrlLine.Matches.Groups[1].Value) {
            return $authUrlLine.Matches.Groups[1].Value.TrimEnd('/')
        }
    }
    return "http://127.0.0.1:8080"
}

function Invoke-SelfUpdate([switch]$Interactive) {
    if ($SkipUpdateCheck) { return }

    $currentVersion = Get-CurrentAppVersion
    $authUrl = Get-AuthServerEndpoint
    $checkUrl = $authUrl + "/api/v1/update/check?current_version=" + $currentVersion + "&channel=stable"

    Write-Host ("Checking for updates (Current: v" + $currentVersion + ")...") -ForegroundColor DarkGray
    try {
        $update = Invoke-RestMethod -Uri $checkUrl -TimeoutSec 5 -ErrorAction Stop
    } catch {
        Write-Host ("Update check skipped (Auth server unreachable at " + $authUrl + ").") -ForegroundColor DarkGray
        return
    }

    if (-not $update.update_available) {
        Write-Host ("Content Bot is up to date (v" + $currentVersion + ").") -ForegroundColor Green
        return
    }

    Write-Host ""
    Write-Host "=================================================================" -ForegroundColor Cyan
    Write-Host ("   NEW UPDATE AVAILABLE: v" + $update.latest_version + " (Current: v" + $currentVersion + ")") -ForegroundColor Yellow
    Write-Host "=================================================================" -ForegroundColor Cyan
    if ($update.changelog) {
        Write-Host ""
        Write-Host "Changelog:" -ForegroundColor White
        Write-Host $update.changelog -ForegroundColor Gray
    }
    Write-Host ""

    if (-not $ForceUpdate -and -not $update.mandatory) {
        $prompt = Read-Host "Do you want to download and install this update now? (Y/n)"
        if ($prompt -and $prompt -notmatch '^[Yy]') {
            Write-Host "Update skipped. Starting application..." -ForegroundColor DarkGray
            return
        }
    }

    $updatesDir = Join-Path $taskRoot "data\updates"
    $backupsDir = Join-Path $taskRoot "data\backups"
    $zipFile = Join-Path $updatesDir ("content-bot-" + $update.latest_version + ".zip")
    $extractedDir = Join-Path $updatesDir "extracted"

    New-Item -ItemType Directory -Path $updatesDir -Force | Out-Null
    New-Item -ItemType Directory -Path $backupsDir -Force | Out-Null

    Write-Host "Downloading update package from Cloudflare R2 / Storage..." -ForegroundColor Cyan
    Write-Host ("Download URL: " + $update.download_url) -ForegroundColor DarkGray
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13
        $webClient = New-Object System.Net.WebClient
        $webClient.DownloadFile($update.download_url, $zipFile)
    } catch {
        Write-Error ("Failed to download update: " + $_.Exception.Message)
        return
    }

    Write-Host "Verifying package SHA256 integrity..." -ForegroundColor Cyan
    $downloadedHash = (Get-FileHash -LiteralPath $zipFile -Algorithm SHA256).Hash.ToLower()
    $expectedHash = $update.sha256.ToLower().Trim()

    if ($downloadedHash -ne $expectedHash) {
        Remove-Item -LiteralPath $zipFile -Force -ErrorAction SilentlyContinue
        throw ("SHA256 mismatch! Expected: " + $expectedHash + " Actual: " + $downloadedHash)
    }
    Write-Host "SHA256 verification passed." -ForegroundColor Green

    # Backup current codebase
    $timestamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
    $backupFolder = Join-Path $backupsDir ("v" + $currentVersion + "-" + $timestamp)
    Write-Host ("Creating backup at " + $backupFolder + "...") -ForegroundColor Cyan
    New-Item -ItemType Directory -Path $backupFolder -Force | Out-Null

    foreach ($folder in "backend", "frontend", "scripts", "auth-server") {
        $sourcePath = Join-Path $taskRoot $folder
        if (Test-Path -LiteralPath $sourcePath) {
            $destPath = Join-Path $backupFolder $folder
            New-Item -ItemType Directory -Path $destPath -Force | Out-Null
            robocopy $sourcePath $destPath /E /XD .venv node_modules __pycache__ .pytest_cache /XF *.pyc .env /NJH /NJS /NDL /NC /NS | Out-Null
        }
    }

    # Extract update package
    if (Test-Path -LiteralPath $extractedDir) {
        Remove-Item -LiteralPath $extractedDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Extracting update archive..." -ForegroundColor Cyan
    Expand-Archive -LiteralPath $zipFile -DestinationPath $extractedDir -Force

    # Overwrite workspace files
    Write-Host "Applying updated source files..." -ForegroundColor Cyan
    robocopy $extractedDir $taskRoot /E /XD .venv node_modules data vendor .git .vscode /XF .env /NJH /NJS /NDL /NC /NS | Out-Null

    # Cleanup temporary update files
    Remove-Item -LiteralPath $extractedDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $zipFile -Force -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Host "Source updated successfully! Refreshing dependencies..." -ForegroundColor Green

    Initialize-BackendEnvironment
    Initialize-FrontendEnvironment

    Write-Host ""
    Write-Host "=================================================================" -ForegroundColor Green
    Write-Host ("   UPDATE COMPLETED SUCCESSFULLY: v" + $update.latest_version + "!") -ForegroundColor Green
    Write-Host "=================================================================" -ForegroundColor Green
    Write-Host ""
}

function Invoke-Rollback {
    $backupsDir = Join-Path $taskRoot "data\backups"
    if (-not (Test-Path -LiteralPath $backupsDir)) {
        throw "No backup directories found in data\backups."
    }

    $backupList = Get-ChildItem -LiteralPath $backupsDir -Directory | Sort-Object CreationTime -Descending
    if (-not $backupList) {
        throw "No previous backups available for rollback."
    }

    $latestBackup = $backupList[0]
    Write-Host ("Latest backup: " + $latestBackup.Name + " (Created: " + $latestBackup.CreationTime + ")") -ForegroundColor Yellow
    $prompt = Read-Host "Do you want to restore this backup? (Y/n)"
    if ($prompt -and $prompt -notmatch '^[Yy]') {
        Write-Host "Rollback cancelled." -ForegroundColor Gray
        return
    }

    Write-Host ("Restoring files from " + $latestBackup.FullName + "...") -ForegroundColor Cyan
    robocopy $latestBackup.FullName $taskRoot /E /XD .venv node_modules data vendor .git /XF .env /NJH /NJS /NDL /NC /NS | Out-Null

    Write-Host "Reinstalling dependencies after rollback..." -ForegroundColor Cyan
    Initialize-BackendEnvironment
    Initialize-FrontendEnvironment

    Write-Host ""
    Write-Host "=================================================================" -ForegroundColor Green
    Write-Host ("   ROLLBACK COMPLETED TO " + $latestBackup.Name + "!") -ForegroundColor Green
    Write-Host "=================================================================" -ForegroundColor Green
}

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

function Initialize-AuthServerEnvironment {
    $python = "auth-server\.venv\Scripts\python.exe"
    $dependencyFile = "auth-server\requirements.txt"
    $stampFile = "auth-server\.venv\.content-bot-dependencies"
    $dependencyHash = (Get-FileHash -LiteralPath $dependencyFile -Algorithm SHA256).Hash
    $installedHash = if (Test-Path -LiteralPath $stampFile) {
        (Get-Content -LiteralPath $stampFile -Raw).Trim()
    } else {
        ""
    }
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Host "Creating the Auth Server virtual environment..."
        python -m venv auth-server\.venv
        if ($LASTEXITCODE -ne 0) { throw "Unable to create the Auth Server environment." }
    }
    if ($installedHash -ne $dependencyHash) {
        Write-Host "Installing changed Auth Server dependencies..."
        & $python -m pip install -r $dependencyFile
        if ($LASTEXITCODE -ne 0) { throw "Unable to install Auth Server dependencies." }
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
        try {
            # Keep the existing development tree and install only lockfile changes.
            # `npm ci` removes every package first, which is unnecessarily slow here
            # and can fail while the Electron development process is still closing.
            npm install --prefer-offline --no-audit --no-fund
            $installExitCode = $LASTEXITCODE
        } finally {
            Pop-Location
        }
        if ($installExitCode -ne 0) { throw "Unable to install frontend dependencies." }
        $electronExecutable = "frontend\node_modules\electron\dist\electron.exe"
        $electronInstaller = "frontend\node_modules\electron\install.js"
        if (-not (Test-Path -LiteralPath $electronExecutable) -and (Test-Path -LiteralPath $electronInstaller)) {
            Write-Host "Completing the Electron runtime download..."
            & node $electronInstaller
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $electronExecutable)) {
                throw "Unable to install the Electron desktop runtime."
            }
        }
        Set-Content -LiteralPath $stampFile -Value $dependencyHash -Encoding ascii
    }
}

function Invoke-Backend {
    if (Test-LocalListener $BackendPort) {
        throw "Port $BackendPort is already in use. Stop the existing backend before starting this VS Code task."
    }
    Initialize-BackendEnvironment
    Write-Host "Starting Content Bot backend in the foreground at http://127.0.0.1:$BackendPort"
    $backendArguments = @(
        "-u", "-m", "uvicorn", "app.main:app",
        "--app-dir", "backend",
        "--host", "127.0.0.1",
        "--port", "$BackendPort"
    )
    if ($env:OS -eq "Windows_NT") {
        # Uvicorn's reload supervisor forces SelectorEventLoop on Windows.
        # Playwright needs ProactorEventLoop to launch the owned Cốc Cốc process.
        Write-Host "Windows Live Wall mode: backend auto-reload is disabled."
    } else {
        $backendArguments += @("--reload", "--reload-dir", "backend\app")
    }
    & backend\.venv\Scripts\python.exe @backendArguments
    if ($LASTEXITCODE -ne 0) { throw "Backend stopped with code $LASTEXITCODE." }
}

function Invoke-AuthServer {
    if (Test-LocalListener $AuthPort) {
        throw "Port $AuthPort is already in use. Stop the existing Auth Server before starting this VS Code task."
    }
    Initialize-AuthServerEnvironment
    Write-Host "Starting Content Bot auth server in the foreground at http://127.0.0.1:$AuthPort"
    & auth-server\.venv\Scripts\python.exe -u -m uvicorn app.main:app `
        --app-dir auth-server `
        --host 127.0.0.1 `
        --port $AuthPort `
        --reload `
        --reload-dir auth-server\app
    if ($LASTEXITCODE -ne 0) { throw "Auth Server stopped with code $LASTEXITCODE." }
}

function Invoke-Frontend {
    if (Test-LocalListener $FrontendPort) {
        throw "Port $FrontendPort is already in use. Stop the existing frontend before starting this VS Code task."
    }
    Initialize-FrontendEnvironment
    Write-Host "Starting Content Bot frontend in the foreground at http://127.0.0.1:$FrontendPort"
    Push-Location frontend
    try {
        npm run dev -- --host 127.0.0.1 --port $FrontendPort --strictPort
        $frontendExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($frontendExitCode -ne 0) { throw "Frontend stopped with code $frontendExitCode." }
}

function Invoke-Desktop {
    Initialize-AuthServerEnvironment
    Initialize-BackendEnvironment
    Initialize-FrontendEnvironment
    Write-Host "Starting Content Bot Desktop development mode."
    Write-Host "React updates reload immediately; desktop process files restart Electron automatically."
    Push-Location frontend
    try {
        npm run desktop:dev
        $desktopExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($desktopExitCode -ne 0) { throw "Desktop development mode stopped with code $desktopExitCode." }
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
        $pythonMinor = [version]((& python -c "import sys; print(str(sys.version_info.major) + '.' + str(sys.version_info.minor))" 2>&1) -join "")
        $pyState = if ($pythonMinor -ge [version]"3.11") { "pass" } else { "fail" }
        Add-Check "Python" $pyState ($pythonVersion + "; 3.11+ required.") $true
    } else {
        Add-Check "Python" "fail" "Python 3.11+ is required." $true
    }

    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if ($nodeCommand) {
        $nodeVersion = ((& node --version 2>&1) -join " ").TrimStart("v")
        $nodeState = if ([version]$nodeVersion -ge [version]"20.0") { "pass" } else { "fail" }
        Add-Check "Node.js" $nodeState ("v" + $nodeVersion + "; 20+ required.") $true
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
        $probeScript = 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop(); print("ok")'
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

    $mongoLocal = Get-NetTCPConnection -LocalPort 27017 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    Add-Check "Local MongoDB" $(if ($mongoLocal) { "pass" } else { "optional" }) $(if ($mongoLocal) { "Listening locally on port 27017 (PID " + $mongoLocal.OwningProcess + ")." } else { "Not detected on port 27017; install with 'winget install MongoDB.Server' or start service 'net start MongoDB'." })

    foreach ($port in 8000, 5173) {
        $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        Add-Check "Port $port" $(if ($listener) { "pass" } else { "idle" }) $(if ($listener) { "Listening locally (PID " + $listener.OwningProcess + ")." } else { "Available." })
    }

    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 3
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/ready" -TimeoutSec 3
        Add-Check "API health" "pass" ("Healthy and ready; " + $health.sources + " sources registered.")
    } catch {
        Add-Check "API health" "fail" "API is not healthy or MongoDB is not ready; inspect the Backend task terminal." $true
    }

    if ($Save) {
        $diagnosticsRoot = Join-Path $taskRoot "data\diagnostics"
        New-Item -ItemType Directory -Path $diagnosticsRoot -Force | Out-Null
        $timestamp = [DateTimeOffset]::Now.ToString("yyyyMMdd-HHmmss-fff")
        $diagnosticsPath = Join-Path $diagnosticsRoot ("doctor-" + $timestamp + ".json")
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
        if ($Save) { Write-Host ("Saved redacted diagnostics: " + $diagnosticsPath) }
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
    $pyCode = 'import pathlib,tomllib; p=pathlib.Path(r"' + $crawlerRoot + '")/"pyproject.toml"; print("\n".join(tomllib.loads(p.read_text(encoding="utf-8"))["project"]["dependencies"]))'
    $dependencies = & $runtime -c $pyCode
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
        "auth" { Invoke-AuthServer }
        "backend" {
            Invoke-SelfUpdate
            Invoke-Backend
        }
        "frontend" { Invoke-Frontend }
        "desktop" { Invoke-Desktop }
        "doctor" { Invoke-Doctor -Json:$Json -Deep:$Deep -Save:$Save }
        "setup-mediacrawler" { Invoke-SetupMediaCrawler }
        "setup-subtitles" { Invoke-SetupSubtitles -Model $SubtitleModel -DownloadModel:$DownloadSubtitleModel }
        "update" { Invoke-SelfUpdate -Interactive }
        "rollback" { Invoke-Rollback }
    }
} catch {
    Write-Error ("Content Bot execution error: " + $_.Exception.Message)
    exit 1
}
