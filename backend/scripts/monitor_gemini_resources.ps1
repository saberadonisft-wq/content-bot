param([Parameter(Mandatory=$true)][int]$TargetProcessId, [Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $OutputDirectory).Path
$started = Get-Date
$cpu = @{}
$peakMemory = 0L
$peakTemporary = 0L
$samples = 0
while (Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue) {
    $processTree = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId)
    $ids = @($TargetProcessId)
    do {
        $previousCount = $ids.Count
        $ids = @($ids + @($processTree | Where-Object { $_.ParentProcessId -in $ids } | ForEach-Object { [int]$_.ProcessId }) | Select-Object -Unique)
    } while ($ids.Count -gt $previousCount)
    $workingSet = 0L
    foreach ($item in @(Get-Process -Id $ids -ErrorAction SilentlyContinue)) {
        $workingSet += $item.WorkingSet64
        $cpu[[string]$item.Id] = [double]$item.CPU
    }
    $peakMemory = [Math]::Max($peakMemory, $workingSet)
    $temporary = 0L
    $jobsRoot = Join-Path $root 'jobs'
    if (Test-Path -LiteralPath $jobsRoot) {
        $temporary = [long](@(Get-ChildItem -LiteralPath $jobsRoot -Directory -Filter 'run-*' | Get-ChildItem -File -Recurse | Measure-Object Length -Sum)[0].Sum)
    }
    $peakTemporary = [Math]::Max($peakTemporary, $temporary)
    $samples++
    @{
        sample_count = $samples
        sampling_interval_seconds = 5
        observed_seconds = ((Get-Date) - $started).TotalSeconds
        observed_cpu_seconds = ($cpu.Values | Measure-Object -Sum).Sum
        peak_tree_working_set_bytes = $peakMemory
        peak_proxy_bytes = $peakTemporary
        note = 'Sampled process tree from monitor start; short-lived processes and final CPU increments can be missed. Working sets may count shared pages more than once. GPU not measured.'
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'resources.json') -Encoding utf8
    Start-Sleep -Seconds 5
}
