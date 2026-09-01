[CmdletBinding()]
param(
    [int[]]$Tiers = @(15, 20, 30, 40, 50, 60),
    [ValidateRange(1, 600)][int]$AudioSeconds = 180,
    [ValidateRange(5, 120)][int]$CooldownSeconds = 60,
    [ValidateRange(1, 30)][int]$SampleIntervalSeconds = 5,
    [ValidateRange(1, 60)][int]$BaselineStable = 10,
    [string]$K6Image = "grafana/k6:0.57.0",
    [ValidatePattern("^[A-Za-z0-9-]+$")][string]$RunLabel = "breakpoint"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$baseCompose = Join-Path $repoRoot "docker-compose.yml"
$testCompose = Join-Path $PSScriptRoot "docker-compose.breakpoint.yml"
$runId = (Get-Date -Format "yyyyMMdd-HHmmss") + "-$RunLabel"
$relativeResultDir = "test-results/load/$runId"
$resultDir = Join-Path $repoRoot ($relativeResultDir -replace "/", [IO.Path]::DirectorySeparatorChar)
$serviceName = "voice-input"
$testToken = "breakpoint-" + [guid]::NewGuid().ToString("N")
$previousLoadToken = [Environment]::GetEnvironmentVariable("LOAD_SERVICE_TOKEN", "Process")
$results = [System.Collections.Generic.List[object]]::new()
$runFailure = $null
$restoreStatus = "not attempted"
$criticalStop = $false
$targetContainerId = $null
$containerCpuLimit = 2.0
$containerMemoryLimitBytes = 4GB
$asrQueueLimit = 12
$polishQueueLimit = 12
$ffmpegQueueLimit = 4

New-Item -ItemType Directory -Force -Path $resultDir | Out-Null

function Invoke-DockerChecked {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Get-TargetContainerId {
    $id = (& docker compose -f $baseCompose -f $testCompose ps -q $serviceName).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $id) {
        throw "Unable to resolve the breakpoint target container."
    }
    return $id
}

function Wait-TargetHealthy {
    param([Parameter(Mandatory = $true)][string]$ContainerId, [int]$TimeoutSeconds = 60)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $inspection = @(& docker inspect $ContainerId | ConvertFrom-Json)[0]
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect target container $ContainerId."
        }
        $status = if ($inspection.State.Health) { $inspection.State.Health.Status } else { $inspection.State.Status }
        if ($status -eq "healthy") {
            return
        }
        if ($inspection.State.Status -eq "exited" -or $inspection.State.OOMKilled) {
            throw "Target container stopped before becoming healthy."
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    throw "Target container did not become healthy within $TimeoutSeconds seconds."
}

function Get-MetricValue {
    param([string]$Metrics, [string]$Pattern)

    $match = [regex]::Match($Metrics, "(?m)^$Pattern ([0-9.eE+-]+)$")
    if ($match.Success) {
        return [double]::Parse($match.Groups[1].Value, [Globalization.CultureInfo]::InvariantCulture)
    }
    return 0.0
}

function Convert-SizeToBytes {
    param([string]$Value)

    $match = [regex]::Match($Value.Trim(), "^([0-9.]+)\s*([KMGT]?i?B)$", "IgnoreCase")
    if (-not $match.Success) {
        throw "Unsupported Docker memory value: $Value"
    }
    $number = [double]::Parse($match.Groups[1].Value, [Globalization.CultureInfo]::InvariantCulture)
    switch ($match.Groups[2].Value.ToUpperInvariant()) {
        "B"   { return $number }
        "KB"  { return $number * 1000 }
        "KIB" { return $number * 1KB }
        "MB"  { return $number * 1000 * 1000 }
        "MIB" { return $number * 1MB }
        "GB"  { return $number * 1000 * 1000 * 1000 }
        "GIB" { return $number * 1GB }
        "TB"  { return $number * 1000 * 1000 * 1000 * 1000 }
        "TIB" { return $number * 1TB }
        default { throw "Unsupported Docker memory unit: $Value" }
    }
}

function Get-HostMemoryPercent {
    $os = Get-CimInstance Win32_OperatingSystem
    if (-not $os.TotalVisibleMemorySize) {
        return 0.0
    }
    return [math]::Round((1.0 - ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize)) * 100, 3)
}

function Get-TargetSample {
    param([Parameter(Mandatory = $true)][string]$ContainerId, [Parameter(Mandatory = $true)][datetime]$StartedAt)

    $statsText = (& docker stats --no-stream --format "{{json .}}" $ContainerId).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $statsText) {
        throw "Unable to read Docker statistics for the target container."
    }
    $stats = $statsText | ConvertFrom-Json
    $cpuRaw = [double]::Parse(($stats.CPUPerc -replace "%", "").Trim(), [Globalization.CultureInfo]::InvariantCulture)
    $memoryUsageText = (($stats.MemUsage -split "/")[0]).Trim()
    $memoryBytes = Convert-SizeToBytes $memoryUsageText
    $metrics = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:9100/metrics").Content
    $inspection = @(& docker inspect $ContainerId | ConvertFrom-Json)[0]
    $health = if ($inspection.State.Health) { $inspection.State.Health.Status } else { $inspection.State.Status }

    return [pscustomobject]@{
        timestamp = Get-Date -Format "o"
        elapsed_seconds = [math]::Round(((Get-Date) - $StartedAt).TotalSeconds, 1)
        cpu_docker_percent = $cpuRaw
        cpu_two_core_percent = [math]::Round($cpuRaw / $containerCpuLimit, 3)
        container_memory_bytes = [math]::Round($memoryBytes)
        container_memory_mib = [math]::Round($memoryBytes / 1MB, 3)
        container_memory_limit_percent = [math]::Round(($memoryBytes / $containerMemoryLimitBytes) * 100, 3)
        host_memory_percent = Get-HostMemoryPercent
        pids = [int]$stats.PIDs
        active_sessions = Get-MetricValue $metrics 'voice_active_sessions\{transport="websocket"\}'
        admission_queue = Get-MetricValue $metrics 'voice_admission_queue_depth'
        asr_inflight = Get-MetricValue $metrics 'voice_asr_inflight'
        asr_queue = Get-MetricValue $metrics 'voice_asr_queue_depth'
        polish_inflight = Get-MetricValue $metrics 'voice_polish_inflight'
        polish_queue = Get-MetricValue $metrics 'voice_polish_queue_depth'
        ffmpeg_inflight = Get-MetricValue $metrics 'voice_ffmpeg_inflight'
        ffmpeg_queue = Get-MetricValue $metrics 'voice_ffmpeg_queue_depth'
        health = $health
        restart_count = [int]$inspection.RestartCount
        oom_killed = [bool]$inspection.State.OOMKilled
    }
}

function Test-AllWorkCleared {
    param([Parameter(Mandatory = $true)]$Sample)

    return (($Sample.active_sessions + $Sample.admission_queue + $Sample.asr_inflight + $Sample.asr_queue +
        $Sample.polish_inflight + $Sample.polish_queue + $Sample.ffmpeg_inflight + $Sample.ffmpeg_queue) -eq 0)
}

function Wait-ForNextSample {
    param([Parameter(Mandatory = $true)][datetime]$SampleStartedAt)

    $remainingMilliseconds = ($SampleIntervalSeconds * 1000) - ((Get-Date) - $SampleStartedAt).TotalMilliseconds
    if ($remainingMilliseconds -gt 0) {
        Start-Sleep -Milliseconds ([int][math]::Ceiling($remainingMilliseconds))
    }
}

function Get-Percentile95 {
    param([object[]]$Values)

    $numbers = @($Values | ForEach-Object { [double]$_ } | Sort-Object)
    if ($numbers.Count -eq 0) {
        return 0.0
    }
    return $numbers[[math]::Ceiling($numbers.Count * 0.95) - 1]
}

function Get-Maximum {
    param([object[]]$Values)

    $measurement = $Values | ForEach-Object { [double]$_ } | Measure-Object -Maximum
    if ($null -eq $measurement.Maximum) {
        return 0.0
    }
    return [double]$measurement.Maximum
}

function Get-Median {
    param([object[]]$Values)

    $numbers = @($Values | ForEach-Object { [double]$_ } | Sort-Object)
    if ($numbers.Count -eq 0) {
        return 0.0
    }
    $middle = [math]::Floor($numbers.Count / 2)
    if (($numbers.Count % 2) -eq 1) {
        return $numbers[$middle]
    }
    return ($numbers[$middle - 1] + $numbers[$middle]) / 2
}

function Test-SustainedQueueGrowth {
    param([object[]]$Rows, [string]$PropertyName)

    $activeRows = @($Rows | Where-Object { $_.active_sessions -gt 0 })
    $windowSize = [math]::Max(4, [math]::Ceiling(30 / $SampleIntervalSeconds))
    if ($activeRows.Count -lt $windowSize) {
        return $false
    }
    for ($start = 0; $start -le $activeRows.Count - $windowSize; $start += 1) {
        $values = @($activeRows[$start..($start + $windowSize - 1)] | ForEach-Object { [double]($_.$PropertyName) })
        $nonDecreasing = $true
        $strictIncreases = 0
        for ($index = 1; $index -lt $values.Count; $index += 1) {
            if ($values[$index] -lt $values[$index - 1]) {
                $nonDecreasing = $false
                break
            }
            if ($values[$index] -gt $values[$index - 1]) {
                $strictIncreases += 1
            }
        }
        if ($nonDecreasing -and $strictIncreases -ge 3 -and ($values[-1] - $values[0]) -ge 3) {
            return $true
        }
    }
    return $false
}

function Get-MemoryGrowthBytes {
    param([object[]]$Rows)

    $steady = @($Rows | Where-Object { $_.active_sessions -gt 0 -and $_.elapsed_seconds -ge 30 })
    if ($steady.Count -lt 8) {
        return 0.0
    }
    $quarter = [math]::Max(3, [math]::Floor($steady.Count / 4))
    $early = Get-Median @($steady | Select-Object -First $quarter | ForEach-Object { $_.container_memory_bytes })
    $late = Get-Median @($steady | Select-Object -Last $quarter | ForEach-Object { $_.container_memory_bytes })
    return $late - $early
}

function Get-SummaryMetric {
    param($Summary, [string]$MetricName, [string]$FieldName, $Default = 0)

    $metricProperty = $Summary.metrics.PSObject.Properties[$MetricName]
    if ($null -eq $metricProperty) {
        return $Default
    }
    $fieldProperty = $metricProperty.Value.PSObject.Properties[$FieldName]
    if ($null -eq $fieldProperty) {
        return $Default
    }
    return $fieldProperty.Value
}

function Stop-LoadContainer {
    param([string]$Name)

    $exists = (& docker ps -a --filter "name=^/$Name$" --format "{{.Names}}").Trim()
    if ($exists -eq $Name) {
        & docker stop --time 2 $Name | Out-Null
    }
}

function Remove-LoadContainer {
    param([string]$Name)

    $exists = (& docker ps -a --filter "name=^/$Name$" --format "{{.Names}}").Trim()
    if ($exists -eq $Name) {
        & docker rm -f $Name | Out-Null
    }
}

function Save-TargetLogs {
    param([string]$ContainerId, [string]$OutputPath)

    if (-not $ContainerId) {
        return
    }
    $logErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $logLines = & docker logs $ContainerId 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $logErrorPreference
    if ($exitCode -eq 0) {
        $logLines | Set-Content -LiteralPath $OutputPath -Encoding UTF8
    }
}

function Invoke-Tier {
    param([Parameter(Mandatory = $true)][int]$Tier, [Parameter(Mandatory = $true)][string]$TargetContainerId)

    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Starting $Tier VU x $AudioSeconds seconds."
    $tierStarted = Get-Date
    $loadName = "lili-breakpoint-$($runId.ToLowerInvariant())-${Tier}vu"
    $summaryRelative = "$relativeResultDir/k6-summary-${Tier}vu.json"
    $summaryPath = Join-Path $resultDir "k6-summary-${Tier}vu.json"
    $csvPath = Join-Path $resultDir "container-stats-${Tier}vu.csv"
    $logPath = Join-Path $resultDir "k6-${Tier}vu.log"
    $rows = [System.Collections.Generic.List[object]]::new()
    $criticalReason = $null
    $cpuCriticalSamples = 0
    $clearObserved = $false
    $clearSeconds = $null
    $k6ExitCode = -1

    $dockerArguments = @(
        "run", "-d", "--name", $loadName,
        "-v", "${repoRoot}:/work", "-w", "/work",
        $K6Image, "run",
        "-e", "BASE_HTTP=http://host.docker.internal:9100",
        "-e", "BASE_WS=ws://host.docker.internal:9100",
        "-e", "ORIGIN=http://localhost:5173",
        "-e", "SERVICE_TOKEN=$testToken",
        "-e", "ONE_SHOT=true",
        "-e", "VUS=$Tier",
        "-e", "AUDIO_SECONDS=$AudioSeconds",
        "--summary-export=/work/$summaryRelative",
        "load/k6-websocket.js"
    )

    try {
        $loadId = (& docker @dockerArguments).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $loadId) {
            throw "Unable to start the k6 container for $Tier VU."
        }

        while ($true) {
            $sampleStartedAt = Get-Date
            try {
                $sample = Get-TargetSample -ContainerId $TargetContainerId -StartedAt $tierStarted
                $rows.Add($sample)
            }
            catch {
                $criticalReason = "monitoring or health probe failed: $($_.Exception.Message)"
                Stop-LoadContainer $loadName
                break
            }

            if ($sample.cpu_two_core_percent -ge 90) {
                $cpuCriticalSamples += 1
            }
            else {
                $cpuCriticalSamples = 0
            }
            if ($cpuCriticalSamples -ge 3) {
                $criticalReason = "two-core CPU stayed at or above 90% for three samples"
            }
            elseif ($sample.container_memory_limit_percent -ge 85) {
                $criticalReason = "container memory reached 85% of the 4 GiB limit"
            }
            elseif ($sample.health -ne "healthy") {
                $criticalReason = "target health changed to $($sample.health)"
            }
            elseif ($sample.restart_count -gt 0 -or $sample.oom_killed) {
                $criticalReason = "target restarted or was OOM-killed"
            }
            if ($criticalReason) {
                Stop-LoadContainer $loadName
                break
            }

            $loadInspection = @(& docker inspect $loadName | ConvertFrom-Json)[0]
            if (-not $loadInspection.State.Running) {
                break
            }
            Wait-ForNextSample -SampleStartedAt $sampleStartedAt
        }

        $loadInspection = @(& docker inspect $loadName | ConvertFrom-Json)[0]
        $k6ExitCode = [int]$loadInspection.State.ExitCode
        $logErrorPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $logLines = & docker logs $loadName 2>&1
        $ErrorActionPreference = $logErrorPreference
        $logLines | Set-Content -LiteralPath $logPath -Encoding UTF8

        $cooldownStarted = Get-Date
        $consecutiveClearSamples = 0
        while (((Get-Date) - $cooldownStarted).TotalSeconds -le $CooldownSeconds) {
            $sampleStartedAt = Get-Date
            try {
                $sample = Get-TargetSample -ContainerId $TargetContainerId -StartedAt $tierStarted
                $rows.Add($sample)
            }
            catch {
                if (-not $criticalReason) {
                    $criticalReason = "cooldown monitoring failed: $($_.Exception.Message)"
                }
                break
            }
            if (Test-AllWorkCleared $sample) {
                $consecutiveClearSamples += 1
                if ($consecutiveClearSamples -ge 2) {
                    $clearObserved = $true
                    $clearSeconds = [math]::Round(((Get-Date) - $cooldownStarted).TotalSeconds, 1)
                    break
                }
            }
            else {
                $consecutiveClearSamples = 0
            }
            Wait-ForNextSample -SampleStartedAt $sampleStartedAt
        }
    }
    finally {
        $rows | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8
        Remove-LoadContainer $loadName
    }

    $failureReasons = [System.Collections.Generic.List[string]]::new()
    if ($criticalReason) {
        $failureReasons.Add("critical: $criticalReason")
    }
    if (-not (Test-Path -LiteralPath $summaryPath)) {
        $failureReasons.Add("k6 summary was not created")
        $summary = $null
    }
    else {
        $summary = Get-Content -Raw -Encoding UTF8 -LiteralPath $summaryPath | ConvertFrom-Json
    }

    $ready = if ($summary) { [int](Get-SummaryMetric $summary "voice_ready_events" "count" 0) } else { 0 }
    $finals = if ($summary) { [int](Get-SummaryMetric $summary "voice_non_empty_finals" "count" 0) } else { 0 }
    $errors = if ($summary) { [int](Get-SummaryMetric $summary "voice_server_errors" "count" 0) } else { 0 }
    $capacityErrors = if ($summary) { [int](Get-SummaryMetric $summary "voice_capacity_errors" "count" 0) } else { 0 }
    $queueTimeouts = if ($summary) { [int](Get-SummaryMetric $summary "voice_queue_timeouts" "count" 0) } else { 0 }
    $checkFails = if ($summary) { [int](Get-SummaryMetric $summary "checks" "fails" 0) } else { $Tier * 2 }
    $missingLatencyMs = 1000000000.0
    $finalP95 = if ($summary) { [double](Get-SummaryMetric $summary "voice_stop_to_final_ms" "p(95)" $missingLatencyMs) } else { $missingLatencyMs }
    $finalMax = if ($summary) { [double](Get-SummaryMetric $summary "voice_stop_to_final_ms" "max" $missingLatencyMs) } else { $missingLatencyMs }
    $cpuP95 = Get-Percentile95 @($rows | ForEach-Object { $_.cpu_two_core_percent })
    $cpuMax = Get-Maximum @($rows | ForEach-Object { $_.cpu_two_core_percent })
    $memoryMaxBytes = Get-Maximum @($rows | ForEach-Object { $_.container_memory_bytes })
    $hostMemoryMax = Get-Maximum @($rows | ForEach-Object { $_.host_memory_percent })
    $activeMax = Get-Maximum @($rows | ForEach-Object { $_.active_sessions })
    $admissionMax = Get-Maximum @($rows | ForEach-Object { $_.admission_queue })
    $asrMax = Get-Maximum @($rows | ForEach-Object { $_.asr_queue })
    $polishMax = Get-Maximum @($rows | ForEach-Object { $_.polish_queue })
    $ffmpegMax = Get-Maximum @($rows | ForEach-Object { $_.ffmpeg_queue })
    $memoryGrowthBytes = Get-MemoryGrowthBytes @($rows)
    $queueGrowth = (Test-SustainedQueueGrowth @($rows) "asr_queue") -or
        (Test-SustainedQueueGrowth @($rows) "polish_queue") -or
        (Test-SustainedQueueGrowth @($rows) "ffmpeg_queue")

    if ($k6ExitCode -ne 0) { $failureReasons.Add("k6 exit code was $k6ExitCode") }
    if ($ready -ne $Tier) { $failureReasons.Add("ready count was $ready/$Tier") }
    if ($finals -ne $Tier) { $failureReasons.Add("non-empty final count was $finals/$Tier") }
    if ($checkFails -ne 0) { $failureReasons.Add("k6 checks failed: $checkFails") }
    if ($errors -ne 0) { $failureReasons.Add("server errors: $errors") }
    if ($capacityErrors -ne 0) { $failureReasons.Add("capacity errors: $capacityErrors") }
    if ($queueTimeouts -ne 0) { $failureReasons.Add("queue timeouts: $queueTimeouts") }
    if ($finalP95 -ge 10000) { $failureReasons.Add("stop-to-final P95 was $([math]::Round($finalP95, 1)) ms") }
    if ($finalMax -ge 20000) { $failureReasons.Add("stop-to-final max was $([math]::Round($finalMax, 1)) ms") }
    if ($cpuP95 -ge 70) { $failureReasons.Add("two-core CPU P95 was $([math]::Round($cpuP95, 3))%") }
    if ($memoryMaxBytes -ge 2GB) { $failureReasons.Add("container memory reached $([math]::Round($memoryMaxBytes / 1MB, 1)) MiB") }
    if ($admissionMax -gt 0) { $failureReasons.Add("admission queue was unexpectedly used") }
    if ($asrMax -ge $asrQueueLimit) { $failureReasons.Add("ASR queue reached its configured limit") }
    if ($polishMax -ge $polishQueueLimit) { $failureReasons.Add("polish queue reached its configured limit") }
    if ($ffmpegMax -ge $ffmpegQueueLimit) { $failureReasons.Add("FFmpeg queue reached its configured limit") }
    if ($queueGrowth) { $failureReasons.Add("an internal queue grew continuously for at least 30 seconds") }
    if ($memoryGrowthBytes -ge 128MB) { $failureReasons.Add("steady-state container memory grew by at least 128 MiB") }
    if (-not $clearObserved) { $failureReasons.Add("connections and queues did not clear within $CooldownSeconds seconds") }
    if (@($rows | Where-Object { $_.health -ne "healthy" -or $_.restart_count -gt 0 -or $_.oom_killed }).Count -gt 0) {
        $failureReasons.Add("target health, restart, or OOM invariant failed")
    }

    $passed = $failureReasons.Count -eq 0
    $result = [pscustomobject]@{
        tier = $Tier
        passed = $passed
        critical = [bool]$criticalReason
        ready = $ready
        finals = $finals
        errors = $errors
        capacity_errors = $capacityErrors
        queue_timeouts = $queueTimeouts
        check_fails = $checkFails
        final_p95_ms = [math]::Round($finalP95, 1)
        final_max_ms = [math]::Round($finalMax, 1)
        cpu_p95_percent = [math]::Round($cpuP95, 3)
        cpu_max_percent = [math]::Round($cpuMax, 3)
        container_memory_max_mib = [math]::Round($memoryMaxBytes / 1MB, 1)
        host_memory_max_percent = [math]::Round($hostMemoryMax, 3)
        active_max = $activeMax
        admission_queue_max = $admissionMax
        asr_queue_max = $asrMax
        polish_queue_max = $polishMax
        ffmpeg_queue_max = $ffmpegMax
        clear_seconds = $clearSeconds
        failure_reasons = @($failureReasons)
    }
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $resultDir "result-${Tier}vu.json") -Encoding UTF8
    $statusText = if ($passed) { "PASS" } else { "FAIL" }
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $Tier VU $statusText; finals=$finals/$Tier, final_max=$($result.final_max_ms)ms, CPU_P95=$($result.cpu_p95_percent)%, memory_max=$($result.container_memory_max_mib)MiB, clear=$clearSeconds s."
    if (-not $passed) {
        Write-Host "Reasons: $($failureReasons -join '; ')"
    }
    return $result
}

function Write-FinalReport {
    param([int]$Stable, [Nullable[int]]$Failed, [string]$RestoreResult, [string]$FatalError)

    $report = [Text.StringBuilder]::new()
    [void]$report.AppendLine("# 15-to-60 VU adaptive breakpoint load-test result")
    [void]$report.AppendLine()
    [void]$report.AppendLine("- Run ID: $runId")
    [void]$report.AppendLine("- Verified stable tier: N_stable=$Stable")
    if ($null -ne $Failed) {
        [void]$report.AppendLine("- First failed tier: N_fail=$Failed")
        [void]$report.AppendLine("- Breakpoint interval: $Stable to $Failed VU")
    }
    else {
        [void]$report.AppendLine("- N_fail: not found")
        [void]$report.AppendLine("- Conclusion: the breakpoint is above $Stable VU")
    }
    [void]$report.AppendLine("- Normal configuration restore: $RestoreResult")
    if ($FatalError) {
        [void]$report.AppendLine("- Automation error: $FatalError")
    }
    [void]$report.AppendLine()
    [void]$report.AppendLine("| VU | Result | Final | Final P95 | Final max | CPU P95 | Container memory max | Host memory max | ASR/Polish/FFmpeg queue max | Cleared |")
    [void]$report.AppendLine("| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |")
    foreach ($item in ($results | Sort-Object tier)) {
        $status = if ($item.passed) { "PASS" } else { "FAIL" }
        $clear = if ($null -eq $item.clear_seconds) { "not cleared" } else { "$($item.clear_seconds) s" }
        [void]$report.AppendLine("| $($item.tier) | $status | $($item.finals)/$($item.tier) | $($item.final_p95_ms) ms | $($item.final_max_ms) ms | $($item.cpu_p95_percent)% | $($item.container_memory_max_mib) MiB | $($item.host_memory_max_percent)% | $($item.asr_queue_max)/$($item.polish_queue_max)/$($item.ffmpeg_queue_max) | $clear |")
    }
    [void]$report.AppendLine()
    [void]$report.AppendLine("## Decision")
    [void]$report.AppendLine()
    if ($Stable -gt 0) {
        $rLow = [math]::Floor($Stable * 0.70)
        $rHigh = [math]::Floor($Stable * 0.80)
        [void]$report.AppendLine("- Mock candidate rated capacity: R=$rLow to $rHigh.")
        [void]$report.AppendLine("- Mock candidate hard limit: C must not exceed $Stable.")
    }
    [void]$report.AppendLine("- Do not change production capacity before real-provider validation.")
    [void]$report.AppendLine("- The local load generator and target shared a Windows host; this is not proof for a real 2-core/4-GB cloud host.")
    if (@($results | Where-Object { $_.host_memory_max_percent -ge 85 }).Count -gt 0) {
        [void]$report.AppendLine("- Warning: shared Windows host memory was at least 85%; it was recorded as an environment limitation, not used as a target-container stop condition.")
    }
    [void]$report.AppendLine()
    [void]$report.AppendLine("## Failure reasons")
    [void]$report.AppendLine()
    $failedItems = @($results | Where-Object { -not $_.passed })
    if ($failedItems.Count -eq 0) {
        [void]$report.AppendLine("No tier failed.")
    }
    else {
        foreach ($item in $failedItems) {
            [void]$report.AppendLine("- $($item.tier) VU: $($item.failure_reasons -join '; ')")
        }
    }
    [void]$report.AppendLine()
    [void]$report.AppendLine("This directory contains each tier's k6 summary, k6 log, five-second monitoring CSV, machine-readable decision JSON, and the target-container log when available.")
    $report.ToString() | Set-Content -LiteralPath (Join-Path $resultDir "result.md") -Encoding UTF8
}

$lastStable = $BaselineStable
$firstFailed = $null

try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker CLI is unavailable."
    }
    $hostMemoryBaseline = Get-HostMemoryPercent
    $env:LOAD_SERVICE_TOKEN = $testToken
    Write-Host "Evidence directory: $resultDir"
    Invoke-DockerChecked @("compose", "-f", $baseCompose, "-f", $testCompose, "config", "--quiet")
    Invoke-DockerChecked @("compose", "-f", $baseCompose, "-f", $testCompose, "up", "-d", "--force-recreate", $serviceName)
    $targetContainerId = Get-TargetContainerId
    Wait-TargetHealthy -ContainerId $targetContainerId

    $targetInspection = @(& docker inspect $targetContainerId | ConvertFrom-Json)[0]
    $environment = @($targetInspection.Config.Env)
    $metadata = [ordered]@{
        run_id = $runId
        started_at = Get-Date -Format "o"
        tiers = $Tiers
        baseline_stable = $BaselineStable
        audio_seconds = $AudioSeconds
        cooldown_seconds = $CooldownSeconds
        sample_interval_seconds = $SampleIntervalSeconds
        target_container_id = $targetContainerId
        target_image = $targetInspection.Image
        container_cpu_limit = $containerCpuLimit
        container_memory_limit_bytes = $containerMemoryLimitBytes
        uvicorn_workers = 1
        redis_enabled = $false
        mock_providers_enabled = $true
        mock_provider_delay_ms = 1000
        capacity_c = 60
        admission_queue_q = 0
        asr_concurrency = 3
        asr_queue_limit = $asrQueueLimit
        polish_concurrency = 3
        polish_queue_limit = $polishQueueLimit
        ffmpeg_concurrency = 1
        ffmpeg_queue_limit = $ffmpegQueueLimit
        host_and_load_generator_shared = $true
        host_memory_baseline_percent = $hostMemoryBaseline
    }
    $metadata | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $resultDir "metadata.json") -Encoding UTF8

    foreach ($tier in $Tiers) {
        if ($tier -le $lastStable) {
            continue
        }
        $tierResult = Invoke-Tier -Tier $tier -TargetContainerId $targetContainerId
        $results.Add($tierResult)
        if ($tierResult.passed) {
            $lastStable = $tier
            continue
        }
        $firstFailed = $tier
        if ($tierResult.critical) {
            $criticalStop = $true
        }
        break
    }

    if ($null -ne $firstFailed -and -not $criticalStop -and ($firstFailed - $lastStable) -gt 5) {
        $midpoint = [math]::Floor((($firstFailed + $lastStable) / 2) / 5) * 5
        if ($midpoint -gt $lastStable -and $midpoint -lt $firstFailed) {
            Write-Host "Refining the breakpoint with $midpoint VU."
            $midpointResult = Invoke-Tier -Tier $midpoint -TargetContainerId $targetContainerId
            $results.Add($midpointResult)
            if ($midpointResult.passed) {
                $lastStable = $midpoint
            }
            else {
                $firstFailed = $midpoint
                if ($midpointResult.critical) {
                    $criticalStop = $true
                }
            }
        }
    }
}
catch {
    $runFailure = "$($_.Exception.Message) (line $($_.InvocationInfo.ScriptLineNumber))"
    Write-Warning $runFailure
}
finally {
    try {
        try {
            Save-TargetLogs -ContainerId $targetContainerId -OutputPath (Join-Path $resultDir "target-container.log")
        }
        catch {
            Write-Warning "Unable to save target-container logs: $($_.Exception.Message)"
        }
        if ($null -eq $previousLoadToken) {
            Remove-Item Env:LOAD_SERVICE_TOKEN -ErrorAction SilentlyContinue
        }
        else {
            $env:LOAD_SERVICE_TOKEN = $previousLoadToken
        }
        Invoke-DockerChecked @("compose", "-f", $baseCompose, "up", "-d", "--force-recreate", $serviceName)
        $normalContainerId = (& docker compose -f $baseCompose ps -q $serviceName).Trim()
        Wait-TargetHealthy -ContainerId $normalContainerId
        $normalInspection = @(& docker inspect $normalContainerId | ConvertFrom-Json)[0]
        $normalEnvironment = @($normalInspection.Config.Env)
        $testTokenActive = $normalEnvironment -contains "SERVICE_TOKEN=$testToken"
        $mockSetting = @($normalEnvironment | Where-Object { $_ -like "MOCK_PROVIDERS_ENABLED=*" })
        if ($testTokenActive -or ($mockSetting -contains "MOCK_PROVIDERS_ENABLED=true")) {
            throw "Temporary breakpoint settings remained active after restore."
        }
        $restoreStatus = "successful; service healthy; temporary Mock settings and token removed"
    }
    catch {
        $restoreStatus = "failed: $($_.Exception.Message)"
        if (-not $runFailure) {
            $runFailure = "normal configuration restore failed"
        }
    }
}

Write-FinalReport -Stable $lastStable -Failed $firstFailed -RestoreResult $restoreStatus -FatalError $runFailure
Write-Host "Final report: $(Join-Path $resultDir 'result.md')"

if ($runFailure) {
    throw $runFailure
}
