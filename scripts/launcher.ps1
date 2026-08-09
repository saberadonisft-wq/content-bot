#requires -Version 5.1

<##
    Standalone Content Bot launcher.
    Run with: .\scripts\launcher.ps1
##>

param(
    [ValidateSet("start", "setup-mediacrawler", "setup-subtitles")]
    [string]$Action = "start",
    [ValidateSet("tiny", "base", "small", "medium", "large-v3")]
    [string]$SubtitleModel = "small",
    [switch]$DownloadSubtitleModel
)

$ErrorActionPreference = "Stop"
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot

function Test-LocalListener([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Test-ApiReady {
    try {
        $ready = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/ready" -TimeoutSec 3
        return $ready.status -eq "ready"
    } catch {
        return $false
    }
}

function Get-ManagedApiProcesses([int[]]$RootPids) {
    $processTable = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $knownPids = [System.Collections.Generic.HashSet[int]]::new()
    $pendingPids = [System.Collections.Generic.Queue[int]]::new()

    foreach ($rootPid in $RootPids | Where-Object { $_ -gt 0 } | Select-Object -Unique) {
        $pendingPids.Enqueue($rootPid)
    }

    while ($pendingPids.Count -gt 0) {
        $parentPid = $pendingPids.Dequeue()
        foreach ($process in $processTable | Where-Object { $_.ParentProcessId -eq $parentPid }) {
            if ($knownPids.Add([int]$process.ProcessId)) {
                $pendingPids.Enqueue([int]$process.ProcessId)
            }
        }
    }

    $allPids = @($RootPids + @($knownPids)) | Where-Object { $_ -gt 0 } | Select-Object -Unique
    @($processTable | Where-Object { $_.ProcessId -in $allPids })
}

function Stop-ManagedApi([int[]]$RootPids) {
    $processes = @(Get-ManagedApiProcesses $RootPids)
    foreach ($process in $processes | Sort-Object ProcessId -Descending) {
        $commandLine = [string]$process.CommandLine
        if ($commandLine.Contains("uvicorn app.main:app", [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-BackendPython {
    $python = Join-Path $taskRoot "backend\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Backend environment is missing. Start Content Bot first."
    }
    return $python
}

function Invoke-Start([switch]$ApiOnly) {
    $apiStateFile = Join-Path $taskRoot "data\content-bot-api.json"

    if (-not (Test-Path "backend\.venv\Scripts\python.exe")) {
        python -m venv backend\.venv
        & backend\.venv\Scripts\python.exe -m pip install -e backend
        if ($LASTEXITCODE -ne 0) { throw "Unable to install backend dependencies." }
    }

    if (-not $ApiOnly -and -not (Test-Path "frontend\node_modules")) {
        Push-Location frontend
        npm install
        $installExitCode = $LASTEXITCODE
        Pop-Location
        if ($installExitCode -ne 0) { throw "Unable to install frontend dependencies." }
    }

    if ($ApiOnly) {
        if (Test-ApiReady) {
            Write-Host "Content Bot API is already listening at http://127.0.0.1:8000"
            return
        }
        if (Test-LocalListener 8000) {
            throw "Port 8000 is occupied but the Content Bot API is not healthy. Check data\logs\api-stderr.log."
        }
        & backend\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend
        return
    }

    if (-not (Test-ApiReady)) {
        if (Test-LocalListener 8000) {
            throw "Port 8000 is occupied but the Content Bot API is not healthy. Check data\logs\api-stderr.log."
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $apiStateFile) -Force | Out-Null
        $logRoot = Join-Path $taskRoot "data\logs"
        New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
        $apiProcess = Start-Process -FilePath "$taskRoot\backend\.venv\Scripts\python.exe" -ArgumentList "-u", "-m", "uvicorn", "app.main:app", "--app-dir", "backend" -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot "api-stdout.log") -RedirectStandardError (Join-Path $logRoot "api-stderr.log") -PassThru
        [pscustomobject]@{
            launcher_pid = $apiProcess.Id
            listener_pid = $null
            started_at = [DateTimeOffset]::Now.ToString("o")
        } | ConvertTo-Json | Set-Content -LiteralPath $apiStateFile

        $deadline = (Get-Date).AddSeconds(30)
        do {
            Start-Sleep -Milliseconds 250
            $apiReady = Test-ApiReady
            $apiProcess.Refresh()
        } while (-not $apiReady -and -not $apiProcess.HasExited -and (Get-Date) -lt $deadline)

        if (-not $apiReady) {
            if (-not $apiProcess.HasExited) {
                Stop-ManagedApi @($apiProcess.Id)
            }
            Remove-Item -LiteralPath $apiStateFile -Force -ErrorAction SilentlyContinue
            throw "Content Bot API did not become healthy. Read data\logs\api-stderr.log and api-stdout.log."
        }

        $listenerPid = (Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object -First 1).OwningProcess
        [pscustomobject]@{
            launcher_pid = $apiProcess.Id
            listener_pid = $listenerPid
            started_at = [DateTimeOffset]::Now.ToString("o")
        } | ConvertTo-Json | Set-Content -LiteralPath $apiStateFile
        Remove-Item -LiteralPath (Join-Path $taskRoot "data\content-bot-api.pid") -Force -ErrorAction SilentlyContinue
        Write-Host "Content Bot API started at http://127.0.0.1:8000 (listener PID $listenerPid)"
    } else {
        Write-Host "Content Bot API is already listening at http://127.0.0.1:8000"
    }

    if (Test-LocalListener 5173) {
        Write-Host "Content Bot dashboard is already running at http://127.0.0.1:5173"
        return
    }

    Push-Location frontend
    npm run dev
    $frontendExitCode = $LASTEXITCODE
    Pop-Location
    if ($frontendExitCode -ne 0) { throw "Frontend stopped with code $frontendExitCode." }
}

function Invoke-Stop {
    $apiStateFile = Join-Path $taskRoot "data\content-bot-api.json"

    if (-not (Test-Path -LiteralPath $apiStateFile)) {
        Write-Host "No managed Content Bot API state file was found."
        return
    }

    try {
        $apiState = Get-Content -LiteralPath $apiStateFile -Raw | ConvertFrom-Json
        $launcherPid = [int]$apiState.launcher_pid
        $listenerPid = [int]$apiState.listener_pid
    } catch {
        throw "The Content Bot API state file is invalid; refusing to stop any process."
    }

    $managedPids = @($listenerPid, $launcherPid) | Where-Object { $_ -gt 0 } | Select-Object -Unique
    $processes = @($managedPids | ForEach-Object {
        Get-CimInstance Win32_Process -Filter "ProcessId = $_" -ErrorAction SilentlyContinue
    })
    if (-not $processes) {
        Remove-Item -LiteralPath $apiStateFile -Force
        Write-Host "Removed stale API state; no process was running."
        return
    }

    foreach ($process in $processes) {
        $commandLine = [string]$process.CommandLine
        $isWorkspaceApi = $commandLine.Contains($taskRoot, [StringComparison]::OrdinalIgnoreCase) -and
            $commandLine.Contains("uvicorn app.main:app", [StringComparison]::OrdinalIgnoreCase)
        if (-not $isWorkspaceApi) {
            throw "PID $($process.ProcessId) does not match this workspace API; refusing to stop it."
        }
    }

    Stop-ManagedApi $managedPids

    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 200
        $stillRunning = @($managedPids | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    } while ($stillRunning -and (Get-Date) -lt $deadline)

    if ($stillRunning) { throw "Content Bot API did not stop within 10 seconds." }
    Remove-Item -LiteralPath $apiStateFile -Force
    Write-Host "Content Bot API stopped. SQLite data was not changed."
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

    $managedStateFile = "data\content-bot-api.json"
    if (Test-Path -LiteralPath $managedStateFile) {
        try {
            $managedState = Get-Content -LiteralPath $managedStateFile -Raw | ConvertFrom-Json
            $managedPid = [int]$managedState.listener_pid
            $managedProcess = Get-Process -Id $managedPid -ErrorAction SilentlyContinue
            Add-Check "Managed API process" $(if ($managedProcess) { "pass" } else { "setup" }) $(if ($managedProcess) { "Listener PID $managedPid can be stopped from this launcher." } else { "API state file is stale." })
        } catch {
            Add-Check "Managed API process" "setup" "API state file is invalid."
        }
    } else {
        Add-Check "Managed API process" "optional" "API was not started by this launcher, or is not running."
    }

    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 3
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/ready" -TimeoutSec 3
        Add-Check "API health" "pass" "Healthy and ready; $($health.sources) sources registered."
    } catch {
        Add-Check "API health" "fail" "API is not healthy or MongoDB is not ready; read data\logs\api-stderr.log." $true
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

function Invoke-ScanSource([string]$Keyword, [string]$SourceId, [int]$TimeoutSeconds = 660, [switch]$Json) {
    $apiBase = "http://127.0.0.1:8000/api/v1"
    try { Invoke-RestMethod -Uri "$apiBase/ready" -TimeoutSec 3 | Out-Null } catch { throw "Content Bot API is not ready; check MongoDB and data\logs\api-stderr.log." }

    $keywords = Invoke-RestMethod -Uri "$apiBase/keywords" -TimeoutSec 10
    $trackedKeyword = $keywords | Where-Object { $_.name -ieq $Keyword } | Select-Object -First 1
    if (-not $trackedKeyword) { throw "Tracked keyword '$Keyword' was not found." }

    $sources = Invoke-RestMethod -Uri "$apiBase/sources" -TimeoutSec 15
    $source = $sources | Where-Object { $_.id -ieq $SourceId } | Select-Object -First 1
    if (-not $source) { throw "Source '$SourceId' was not found." }
    if ($source.state -ne "ready") { throw "Source '$($source.label)' is $($source.state): $($source.detail)" }
    if ($source.requires_login -and -not $Json) { Write-Host "A visible browser may open for $($source.label)." }

    $body = @{ keyword_id = $trackedKeyword.id; source_ids = @($source.id); trigger = "manual" } | ConvertTo-Json
    $batch = Invoke-RestMethod -Method Post -Uri "$apiBase/runs" -ContentType "application/json" -Body $body
    if (-not $Json) { Write-Host "Batch $($batch.id) started: '$($trackedKeyword.name)' on $($source.label)." }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastProgress = ""
    do {
        Start-Sleep -Seconds 1
        $batch = Invoke-RestMethod -Uri "$apiBase/runs/$($batch.id)" -TimeoutSec 10
        $sourceRun = $batch.source_runs | Select-Object -First 1
        $percent = if ($null -ne $sourceRun.progress_percent) { " $([math]::Round($sourceRun.progress_percent))%" } else { "" }
        $phase = if ($sourceRun.phase) { $sourceRun.phase } else { $sourceRun.state }
        $message = if ($sourceRun.message) { " — $($sourceRun.message)" } else { "" }
        $progress = "$phase$($percent): fetched $($sourceRun.fetched_count), stored $($sourceRun.ingested_count)$message"
        if (-not $Json -and $progress -ne $lastProgress) { Write-Host $progress; $lastProgress = $progress }
    } while ($batch.state -in @("queued", "running") -and (Get-Date) -lt $deadline)

    if ($batch.state -in @("queued", "running")) {
        Invoke-RestMethod -Method Post -Uri "$apiBase/runs/$($batch.id)/cancel" | Out-Null
        throw "Batch timed out after $TimeoutSeconds seconds."
    }

    $result = [pscustomobject]@{
        batch_id = $batch.id
        keyword = $trackedKeyword.name
        source_id = $source.id
        source = $source.label
        state = $batch.state
        fetched = $sourceRun.fetched_count
        stored = $sourceRun.ingested_count
        error = $sourceRun.error_message
    }
    if ($Json) { $result | ConvertTo-Json -Depth 3 } else { $result | Format-List }
    if ($batch.state -ne "succeeded") { throw "Scan ended as '$($batch.state)'." }
}

function Invoke-Backup([string]$DestinationDirectory = "data\backups") {
    $python = Get-BackendPython
    $backupRoot = [IO.Path]::GetFullPath((Join-Path $taskRoot $DestinationDirectory))
    $timestamp = [DateTimeOffset]::Now.ToString("yyyyMMdd-HHmmss-fff")
    $destination = Join-Path $backupRoot "content-bot-$timestamp.json"
    & $python "backend\scripts\backup_mongodb.py" --destination $destination
    if ($LASTEXITCODE -ne 0) { throw "Backup failed." }
    Write-Host "Backup saved to $destination"
}

function Invoke-Restore([string]$Backup) {
    $python = Get-BackendPython
    if (Test-LocalListener 8000) { throw "Stop the API before restoring." }
    $backupPath = [IO.Path]::GetFullPath((Join-Path $taskRoot $Backup))
    if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) { throw "Backup file was not found: $backupPath" }
    & $python "backend\scripts\restore_mongodb.py" --source $backupPath --replace-current
    if ($LASTEXITCODE -ne 0) { throw "Restore failed." }
    Write-Host "Database restored."
}

function Invoke-Prune([int]$Days = 90, [switch]$Apply) {
    $python = Get-BackendPython
    if ($Apply -and (Test-LocalListener 8000)) { throw "Stop the API before pruning." }
    if ($Apply) {
        & $python "backend\scripts\prune_mongodb.py" --days $Days --apply
    } else {
        & $python "backend\scripts\prune_mongodb.py" --days $Days
    }
    if ($LASTEXITCODE -ne 0) { throw "Prune failed." }
}

function Invoke-DeleteLocalData {
    $python = Get-BackendPython
    if (Test-LocalListener 8000) { throw "Stop the API before deleting local data." }
    & $python "backend\scripts\delete_local_data.py" --confirm-delete-local-data
    if ($LASTEXITCODE -ne 0) { throw "Delete local data failed." }
}

function Read-RequiredValue([string]$Prompt) {
    do { $value = (Read-Host $Prompt).Trim() } while ([string]::IsNullOrWhiteSpace($value))
    return $value
}

try {
    if ($Action -eq "setup-mediacrawler") {
        Invoke-SetupMediaCrawler
    } elseif ($Action -eq "setup-subtitles") {
        Invoke-SetupSubtitles -Model $SubtitleModel -DownloadModel:$DownloadSubtitleModel
    } else {
        Invoke-Start
    }
} catch {
    Write-Error "Content Bot could not start: $($_.Exception.Message)"
    exit 1
}
