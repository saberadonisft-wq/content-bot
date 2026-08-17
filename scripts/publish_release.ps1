#requires -Version 5.1
<##
    Packaging and Release Publisher for Content Bot.
    Builds the production release zip, computes SHA-256, and optionally publishes metadata to Auth Server.
##>

param(
    [string]$Version = "",
    [ValidateSet("stable", "beta")]
    [string]$Channel = "stable",
    [string]$Changelog = "",
    [string]$ChangelogFile = "",
    [switch]$Mandatory,
    [string]$DownloadUrl = "",
    [string]$AuthServerUrl = "",
    [string]$AdminToken = "",
    [switch]$PackageOnly
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

function Get-AppVersion {
    $versionFile = Join-Path $repoRoot "backend\app\version.py"
    if (Test-Path -LiteralPath $versionFile) {
        $content = Get-Content -LiteralPath $versionFile -Raw
        if ($content -match 'APP_VERSION\s*=\s*["'']([^"'']+)["'']') {
            return $matches[1].Trim()
        }
    }
    return "0.1.0"
}

if (-not $Version) {
    $Version = Get-AppVersion
}

if ($ChangelogFile -and (Test-Path -LiteralPath $ChangelogFile)) {
    $Changelog = Get-Content -LiteralPath $ChangelogFile -Raw
}

if (-not $Changelog) {
    $Changelog = "Release v" + $Version + " (" + $Channel + " channel)"
}

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ("   CONTENT BOT RELEASE PACKAGER - v" + $Version + " (" + $Channel + ")") -ForegroundColor Yellow
Write-Host "================================================================" -ForegroundColor Cyan

$releasesDir = Join-Path $repoRoot "artifacts\releases"
$stagingDir = Join-Path $releasesDir ("staging-v" + $Version)
$zipFile = Join-Path $releasesDir ("content-bot-v" + $Version + ".zip")

if (Test-Path -LiteralPath $stagingDir) {
    Remove-Item -LiteralPath $stagingDir -Recurse -Force
}
if (Test-Path -LiteralPath $zipFile) {
    Remove-Item -LiteralPath $zipFile -Force
}

New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null

Write-Host "1. Copying project files into release package..." -ForegroundColor Cyan

foreach ($folder in "backend", "frontend", "scripts", "auth-server", "docs") {
    $src = Join-Path $repoRoot $folder
    if (Test-Path -LiteralPath $src) {
        $dst = Join-Path $stagingDir $folder
        New-Item -ItemType Directory -Path $dst -Force | Out-Null
        robocopy $src $dst /E /XD .venv node_modules __pycache__ .pytest_cache .ruff_cache dist data secrets /XF *.pyc .env .env.* *.log /NJH /NJS /NDL /NC /NS | Out-Null
    }
}

foreach ($file in "README.md", "pytest.ini", ".gitignore") {
    $srcFile = Join-Path $repoRoot $file
    if (Test-Path -LiteralPath $srcFile) {
        Copy-Item -LiteralPath $srcFile -Destination (Join-Path $stagingDir $file) -Force
    }
}

Write-Host "2. Compressing into zip archive..." -ForegroundColor Cyan
Compress-Archive -Path (Join-Path $stagingDir "*") -DestinationPath $zipFile -CompressionLevel Optimal -Force

# Clean staging dir
Remove-Item -LiteralPath $stagingDir -Recurse -Force

# Calculate metrics
$fileItem = Get-Item -LiteralPath $zipFile
$fileSize = $fileItem.Length
$fileSizeMb = [math]::Round($fileSize / 1048576, 2)
$sha256 = (Get-FileHash -LiteralPath $zipFile -Algorithm SHA256).Hash.ToLower()

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "   PACKAGE CREATED SUCCESSFULLY!" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ("   File:     " + $zipFile) -ForegroundColor White
Write-Host ("   Size:     " + $fileSize + " bytes (" + $fileSizeMb + " MB)") -ForegroundColor White
Write-Host ("   Hash:     " + $sha256) -ForegroundColor Yellow
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""

if ($PackageOnly) {
    Write-Host "[INFO] PackageOnly specified. Release was packaged without publishing to Auth Server." -ForegroundColor DarkGray
    return
}

# Next steps for admin
Write-Host "Cloudflare R2 Upload Command (example using rclone or AWS CLI):" -ForegroundColor Cyan
Write-Host ("   rclone copy `"" + $zipFile + "`" r2:content-bot-releases/releases/" + $Channel + "/" + $Version + "/content-bot.zip") -ForegroundColor Gray
Write-Host ("   aws s3 cp `"" + $zipFile + "`" s3://content-bot-releases/releases/" + $Channel + "/" + $Version + "/content-bot.zip") -ForegroundColor Gray
Write-Host ""

if ($AdminToken -and $DownloadUrl) {
    $authUrl = if ($AuthServerUrl) { $AuthServerUrl.TrimEnd('/') } else { "http://127.0.0.1:8080" }
    Write-Host ("Publishing release metadata to Auth Server (" + $authUrl + ")...") -ForegroundColor Cyan

    $body = @{
        version = $Version
        channel = $Channel
        download_url = $DownloadUrl
        sha256 = $sha256
        file_size = $fileSize
        changelog = $Changelog
        mandatory = [bool]$Mandatory
    } | ConvertTo-Json

    $headers = @{
        "Authorization" = "Bearer " + $AdminToken
        "Content-Type" = "application/json"
    }

    try {
        $res = Invoke-RestMethod -Uri ($authUrl + "/admin/releases") -Method Post -Headers $headers -Body $body
        Write-Host ("Release v" + $Version + " successfully published to Auth Server!") -ForegroundColor Green
        Write-Host ($res | ConvertTo-Json -Depth 3) -ForegroundColor White
    } catch {
        Write-Error ("Failed to publish release to Auth Server: " + $_.Exception.Message)
    }
} else {
    Write-Host "To publish this release to the Auth Server, execute:" -ForegroundColor Yellow
    Write-Host ("   .\scripts\publish_release.ps1 -Version `"" + $Version + "`" -DownloadUrl `"<R2_DOWNLOAD_URL>`" -AdminToken `"<JWT_TOKEN>`"") -ForegroundColor DarkGray
}
