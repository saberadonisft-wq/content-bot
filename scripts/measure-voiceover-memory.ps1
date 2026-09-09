param(
    [Parameter(Mandatory=$true)][string]$JobId,
    [string]$OutputPath = "artifacts/voiceover/endurance/memory.jsonl"
)
$ErrorActionPreference = 'Stop'
$outputFile = [IO.Path]::GetFullPath($OutputPath)
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($outputFile)) | Out-Null
$observed = $false
while ($true) {
    $workers = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains('worker.py') -and $_.CommandLine.Contains($JobId) })
    if ($workers.Count -eq 0) {
        if ($observed) { break }
        throw "No live worker found for job $JobId"
    }
    $observed = $true
    foreach ($workerInfo in $workers) {
        $workerProcess = Get-Process -Id $workerInfo.ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $workerProcess) { continue }
        $record = [ordered]@{
            timestamp_utc = [DateTime]::UtcNow.ToString('o')
            job_id = $JobId
            process_id = $workerInfo.ProcessId
            working_set_bytes = $workerProcess.WorkingSet64
            private_bytes = $workerProcess.PrivateMemorySize64
            peak_working_set_bytes = $workerProcess.PeakWorkingSet64
            cpu_seconds = $workerProcess.TotalProcessorTime.TotalSeconds
        }
        Add-Content -LiteralPath $outputFile -Value ($record | ConvertTo-Json -Compress) -Encoding utf8
        Write-Output "Worker $($workerInfo.ProcessId): RAM $([math]::Round($workerProcess.WorkingSet64 / 1MB)) MB"
    }
    Start-Sleep -Seconds 30
}
Write-Output "Worker exited; memory trace saved to $outputFile"
