[CmdletBinding()]
param(
    [ValidateSet("asr", "polish", "e2e")][string]$Stage = "asr",
    [int[]]$Tiers = @(3, 5, 8, 12, 16),
    [ValidateRange(5, 120)][int]$ShortAudioSeconds = 30,
    [ValidateRange(30, 600)][int]$SustainedAudioSeconds = 180,
    [ValidateRange(0, 64)][int]$SustainedOnlyConcurrency = 0,
    [ValidateRange(1, 10)][int]$SustainedRounds = 1,
    [ValidateRange(0, 64)][int]$AsrConcurrency = 0,
    [ValidateRange(0, 64)][int]$PolishConcurrency = 0,
    [ValidateRange(0, 256)][int]$AsrQueueSize = 0,
    [ValidateRange(0, 256)][int]$PolishQueueSize = 0,
    [ValidateRange(0, 5)][int]$SustainedRetries = 2,
    [ValidateRange(0, 120)][int]$SegmentTargetSeconds = 0,
    [ValidateRange(0, 120)][int]$SegmentMaxSeconds = 0,
    [ValidateRange(0, 180)][int]$MixedLoadMinutes = 0,
    [ValidateRange(1, 10)][int]$MixedRounds = 1,
    [string]$MixedAudioDurationDistribution = "5-20:60,20-45:25,45-120:10,120-300:5",
    [ValidateRange(0, 60)][int]$StartJitterSeconds = 5,
    [ValidateRange(10, 120)][int]$CooldownSeconds = 60,
    [ValidateRange(1, 10)][int]$SampleIntervalSeconds = 1,
    [ValidateRange(1, 4)][int]$ProbeWorkers = 1,
    [ValidateRange(0.001, 1.0)][double]$CostBudgetUsd = 0.02,
    [ValidateRange(0.0, 1.0)][double]$AsrPricePerAudioSecondUsd = 0.000003,
    [ValidateNotNullOrEmpty()][string]$ProviderName = "OpenRouter",
    [ValidateNotNullOrEmpty()][string]$AsrModel = "qwen/qwen3-asr-0.6b",
    [ValidateNotNullOrEmpty()][string]$AsrProbeName = "openrouter",
    [ValidateNotNullOrEmpty()][string]$AsrProbeHost = "openrouter.ai",
    [ValidateNotNullOrEmpty()][string]$AsrProbePath = "/api/v1/models",
    [string]$AudioFile = "test-data/voice/zh-30s.pcm",
    [string]$K6Image = "grafana/k6:0.57.0"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$baseCompose = Join-Path $repoRoot "docker-compose.yml"
$testCompose = Join-Path $PSScriptRoot "docker-compose.real-$Stage.yml"
$serviceName = "voice-input"
$runId = (Get-Date -Format "yyyyMMdd-HHmmss") + "-real-$Stage-ladder"
$relativeResultDir = "test-results/load/$runId"
$resultDir = Join-Path $repoRoot ($relativeResultDir -replace "/", [IO.Path]::DirectorySeparatorChar)
$audioPath = Join-Path $repoRoot ($AudioFile -replace "/", [IO.Path]::DirectorySeparatorChar)
$testToken = "real-asr-" + [guid]::NewGuid().ToString("N")
$pricePerAudioSecondUsd = if ($Stage -eq "polish") { 0.0 } else { $AsrPricePerAudioSecondUsd }
$containerCpuLimit = 2.0
$containerMemoryLimitBytes = 4GB
$results = [System.Collections.Generic.List[object]]::new()
$attemptedAudioSeconds = 0
$restoreStatus = "not attempted"
$fatalError = $null
$targetContainerId = $null
$mixedMaxAudioSeconds = 0

if ($MixedLoadMinutes -gt 0) {
    foreach ($rawEntry in $MixedAudioDurationDistribution -split ",") {
        $entry = $rawEntry.Trim()
        $match = [regex]::Match($entry, '^(\d+)-(\d+):(\d+(?:\.\d+)?)$')
        if (-not $match.Success) { throw "Invalid mixed audio duration entry: $entry" }
        $minimum = [int]$match.Groups[1].Value
        $maximum = [int]$match.Groups[2].Value
        $weight = [double]::Parse($match.Groups[3].Value, [Globalization.CultureInfo]::InvariantCulture)
        if ($minimum -le 0 -or $maximum -lt $minimum -or $weight -le 0) {
            throw "Invalid mixed audio duration range: $entry"
        }
        $mixedMaxAudioSeconds = [math]::Max($mixedMaxAudioSeconds, $maximum)
    }
}

$savedEnvironment = @{}
foreach ($name in @(
    "LOAD_SERVICE_TOKEN", "REAL_ASR_CONCURRENCY", "REAL_ASR_QUEUE_SIZE", "REAL_ASR_RETRIES",
    "CAP_ACTIVE_SESSIONS", "CAP_ASR_CONCURRENCY", "CAP_ASR_QUEUE_SIZE", "CAP_ASR_RETRIES",
    "CAP_POLISH_CONCURRENCY", "CAP_POLISH_QUEUE_SIZE", "CAP_SEGMENT_TARGET_SECONDS",
    "CAP_SEGMENT_MAX_SECONDS"
)) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

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
        throw "Unable to resolve the real-ASR target container."
    }
    return $id
}

function Wait-TargetHealthy {
    param([Parameter(Mandatory = $true)][string]$ContainerId, [int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $inspection = @(& docker inspect $ContainerId | ConvertFrom-Json)[0]
        if ($LASTEXITCODE -ne 0) { throw "Unable to inspect target container." }
        $health = if ($inspection.State.Health) { $inspection.State.Health.Status } else { $inspection.State.Status }
        if ($health -eq "healthy") { return }
        if ($inspection.State.Status -eq "exited" -or $inspection.State.OOMKilled) {
            throw "Target exited or was OOM-killed before becoming healthy."
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Target did not become healthy within $TimeoutSeconds seconds."
}

function Get-MetricValue {
    param([string]$Metrics, [string]$Pattern)
    $match = [regex]::Match($Metrics, "(?m)^$Pattern ([0-9.eE+-]+)$")
    if (-not $match.Success) { return 0.0 }
    return [double]::Parse($match.Groups[1].Value, [Globalization.CultureInfo]::InvariantCulture)
}

function Get-AsrStatusCounts {
    param([string]$Metrics)
    $counts = @{}
    foreach ($match in [regex]::Matches($Metrics, '(?m)^voice_asr_requests_total\{status="([^"]+)"\} ([0-9.eE+-]+)$')) {
        $counts[$match.Groups[1].Value] = [int][double]::Parse($match.Groups[2].Value, [Globalization.CultureInfo]::InvariantCulture)
    }
    return $counts
}

function Get-PolishStatusCounts {
    param([string]$Metrics)
    $counts = @{}
    foreach ($match in [regex]::Matches($Metrics, '(?m)^voice_polish_requests_total\{status="([^"]+)"\} ([0-9.eE+-]+)$')) {
        $counts[$match.Groups[1].Value] = [int][double]::Parse($match.Groups[2].Value, [Globalization.CultureInfo]::InvariantCulture)
    }
    return $counts
}

function Convert-SizeToBytes {
    param([string]$Value)
    $match = [regex]::Match($Value.Trim(), "^([0-9.]+)\s*([KMGT]?i?B)$", "IgnoreCase")
    if (-not $match.Success) { throw "Unsupported Docker memory value: $Value" }
    $number = [double]::Parse($match.Groups[1].Value, [Globalization.CultureInfo]::InvariantCulture)
    switch ($match.Groups[2].Value.ToUpperInvariant()) {
        "B" { return $number }
        "KB" { return $number * 1000 }
        "KIB" { return $number * 1KB }
        "MB" { return $number * 1000 * 1000 }
        "MIB" { return $number * 1MB }
        "GB" { return $number * 1000 * 1000 * 1000 }
        "GIB" { return $number * 1GB }
        "TB" { return $number * 1000 * 1000 * 1000 * 1000 }
        "TIB" { return $number * 1TB }
        default { throw "Unsupported Docker memory unit: $Value" }
    }
}

function Get-HostMemoryPercent {
    $os = Get-CimInstance Win32_OperatingSystem
    if (-not $os.TotalVisibleMemorySize) { return 0.0 }
    return [math]::Round((1.0 - ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize)) * 100, 3)
}

function Get-TargetSample {
    param([string]$ContainerId, [datetime]$StartedAt)
    $metrics = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:9100/metrics").Content
    $statsText = (& docker stats --no-stream --format "{{json .}}" $ContainerId).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $statsText) { throw "Unable to read Docker statistics." }
    $stats = $statsText | ConvertFrom-Json
    $cpuRaw = [double]::Parse(($stats.CPUPerc -replace "%", "").Trim(), [Globalization.CultureInfo]::InvariantCulture)
    $memoryBytes = Convert-SizeToBytes ((($stats.MemUsage -split "/")[0]).Trim())
    $netParts = @($stats.NetIO -split "/" | ForEach-Object { $_.Trim() })
    if ($netParts.Count -ne 2) { throw "Unsupported Docker network value: $($stats.NetIO)" }
    $netRxBytes = Convert-SizeToBytes $netParts[0]
    $netTxBytes = Convert-SizeToBytes $netParts[1]
    $inspection = @(& docker inspect $ContainerId | ConvertFrom-Json)[0]
    $health = if ($inspection.State.Health) { $inspection.State.Health.Status } else { $inspection.State.Status }
    return [pscustomobject]@{
        timestamp = Get-Date -Format "o"
        elapsed_seconds = [math]::Round(((Get-Date) - $StartedAt).TotalSeconds, 2)
        cpu_docker_percent = $cpuRaw
        cpu_two_core_percent = [math]::Round($cpuRaw / $containerCpuLimit, 3)
        container_memory_bytes = [math]::Round($memoryBytes)
        container_memory_mib = [math]::Round($memoryBytes / 1MB, 3)
        container_memory_limit_percent = [math]::Round(($memoryBytes / $containerMemoryLimitBytes) * 100, 3)
        net_rx_bytes = [math]::Round($netRxBytes)
        net_tx_bytes = [math]::Round($netTxBytes)
        host_memory_percent = Get-HostMemoryPercent
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
    param($Sample)
    return (($Sample.active_sessions + $Sample.admission_queue + $Sample.asr_inflight + $Sample.asr_queue +
        $Sample.polish_inflight + $Sample.polish_queue + $Sample.ffmpeg_inflight + $Sample.ffmpeg_queue) -eq 0)
}

function Get-Percentile95 {
    param([object[]]$Values)
    $numbers = @($Values | ForEach-Object { [double]$_ } | Sort-Object)
    if ($numbers.Count -eq 0) { return 0.0 }
    return $numbers[[math]::Ceiling($numbers.Count * 0.95) - 1]
}

function Get-Maximum {
    param([object[]]$Values)
    $measurement = $Values | ForEach-Object { [double]$_ } | Measure-Object -Maximum
    if ($null -eq $measurement.Maximum) { return 0.0 }
    return [double]$measurement.Maximum
}

function Get-NetworkSummary {
    param([object[]]$Samples)
    $rxRates = [System.Collections.Generic.List[double]]::new()
    $txRates = [System.Collections.Generic.List[double]]::new()
    for ($index = 1; $index -lt $Samples.Count; $index += 1) {
        $elapsed = [double]$Samples[$index].elapsed_seconds - [double]$Samples[$index - 1].elapsed_seconds
        if ($elapsed -le 0) { continue }
        $rxDelta = [double]$Samples[$index].net_rx_bytes - [double]$Samples[$index - 1].net_rx_bytes
        $txDelta = [double]$Samples[$index].net_tx_bytes - [double]$Samples[$index - 1].net_tx_bytes
        if ($rxDelta -ge 0) { $rxRates.Add(($rxDelta * 8 / 1000000) / $elapsed) }
        if ($txDelta -ge 0) { $txRates.Add(($txDelta * 8 / 1000000) / $elapsed) }
    }
    $rxTotal = if ($Samples.Count -gt 1) { [math]::Max(0, [double]$Samples[-1].net_rx_bytes - [double]$Samples[0].net_rx_bytes) } else { 0 }
    $txTotal = if ($Samples.Count -gt 1) { [math]::Max(0, [double]$Samples[-1].net_tx_bytes - [double]$Samples[0].net_tx_bytes) } else { 0 }
    return [pscustomobject]@{
        rx_average_mbps = if ($rxRates.Count) { [math]::Round((($rxRates | Measure-Object -Average).Average), 4) } else { 0 }
        rx_p95_mbps = [math]::Round((Get-Percentile95 @($rxRates)), 4)
        rx_peak_mbps = [math]::Round((Get-Maximum @($rxRates)), 4)
        tx_average_mbps = if ($txRates.Count) { [math]::Round((($txRates | Measure-Object -Average).Average), 4) } else { 0 }
        tx_p95_mbps = [math]::Round((Get-Percentile95 @($txRates)), 4)
        tx_peak_mbps = [math]::Round((Get-Maximum @($txRates)), 4)
        rx_total_mib = [math]::Round($rxTotal / 1MB, 3)
        tx_total_mib = [math]::Round($txTotal / 1MB, 3)
        recommended_mbps = [math]::Round(([math]::Max((Get-Maximum @($rxRates)), (Get-Maximum @($txRates))) * 1.3), 4)
    }
}

function Get-NetworkProbeSummary {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return [pscustomobject]@{ available = $false } }
    $probeRows = @(Get-Content -LiteralPath $Path | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
    $summary = [ordered]@{ available = $true; samples = $probeRows.Count; targets = [ordered]@{} }
    foreach ($target in @($probeRows.target | Sort-Object -Unique)) {
        $targetRows = @($probeRows | Where-Object { $_.target -eq $target })
        $summary.targets[$target] = [ordered]@{
            samples = $targetRows.Count
            dns_failures = @($targetRows | Where-Object { -not $_.dns_ok }).Count
            tcp_failures = @($targetRows | Where-Object { $_.dns_ok -and -not $_.tcp_ok }).Count
            tls_failures = @($targetRows | Where-Object { $_.tcp_ok -and -not $_.tls_ok }).Count
            http_failures = @($targetRows | Where-Object { $_.tls_ok -and -not $_.http_ok }).Count
            error_kinds = @($targetRows | Where-Object { $_.error_kind } | Group-Object error_kind | ForEach-Object { "$($_.Name)=$($_.Count)" })
        }
    }
    return [pscustomobject]$summary
}

function Get-SummaryMetric {
    param($Summary, [string]$MetricName, [string]$FieldName, $Default = 0)
    $metricProperty = $Summary.metrics.PSObject.Properties[$MetricName]
    if ($null -eq $metricProperty) { return $Default }
    $fieldProperty = $metricProperty.Value.PSObject.Properties[$FieldName]
    if ($null -eq $fieldProperty) { return $Default }
    return $fieldProperty.Value
}

function Remove-LoadContainer {
    param([string]$Name)
    $exists = (& docker ps -a --filter "name=^/$Name$" --format "{{.Names}}").Trim()
    if ($exists -eq $Name) { & docker rm -f $Name | Out-Null }
}

function Save-TargetLogs {
    param([string]$ContainerId, [string]$Path)
    if (-not $ContainerId) { return }
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $lines = & docker logs $ContainerId 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference
    if ($exitCode -eq 0) { $lines | Set-Content -LiteralPath $Path -Encoding UTF8 }
}

function Set-TestConfiguration {
    param([int]$Vus, [int]$Concurrency, [int]$QueueSize, [int]$Retries)
    $env:LOAD_SERVICE_TOKEN = $testToken
    if ($SegmentTargetSeconds -gt 0) { $env:CAP_SEGMENT_TARGET_SECONDS = "$SegmentTargetSeconds" }
    if ($SegmentMaxSeconds -gt 0) { $env:CAP_SEGMENT_MAX_SECONDS = "$SegmentMaxSeconds" }
    if ($Stage -eq "asr") {
        $env:REAL_ASR_CONCURRENCY = "$Concurrency"
        $env:REAL_ASR_QUEUE_SIZE = "$QueueSize"
        $env:REAL_ASR_RETRIES = "$Retries"
    }
    elseif ($Stage -eq "polish") {
        $env:CAP_ACTIVE_SESSIONS = "$Vus"
        $env:CAP_POLISH_CONCURRENCY = "$Concurrency"
        $env:CAP_POLISH_QUEUE_SIZE = "$(if ($PolishQueueSize -gt 0) { $PolishQueueSize } else { [math]::Max($Vus, $Concurrency * 2) })"
    }
    else {
        $effectiveAsr = if ($AsrConcurrency -gt 0) { $AsrConcurrency } else { $Concurrency }
        $effectivePolish = if ($PolishConcurrency -gt 0) { $PolishConcurrency } else { $Concurrency }
        $env:CAP_ACTIVE_SESSIONS = "$Vus"
        $env:CAP_ASR_CONCURRENCY = "$effectiveAsr"
        $env:CAP_ASR_QUEUE_SIZE = "$(if ($AsrQueueSize -gt 0) { $AsrQueueSize } else { [math]::Max($Vus, $effectiveAsr * 2) })"
        $env:CAP_ASR_RETRIES = "$Retries"
        $env:CAP_POLISH_CONCURRENCY = "$effectivePolish"
        $env:CAP_POLISH_QUEUE_SIZE = "$(if ($PolishQueueSize -gt 0) { $PolishQueueSize } else { [math]::Max(0, $Vus - $effectivePolish) })"
    }
    Invoke-DockerChecked @("compose", "-f", $baseCompose, "-f", $testCompose, "up", "-d", "--force-recreate", $serviceName)
    $script:targetContainerId = Get-TargetContainerId
    Wait-TargetHealthy -ContainerId $script:targetContainerId
}

function Invoke-LoadAttempt {
    param(
        [int]$Vus,
        [int]$AudioSeconds = 0,
        [int]$Concurrency,
        [int]$QueueSize,
        [int]$Retries,
        [string]$Label,
        [int]$LoadDurationSeconds = 0,
        [string]$AudioDurationDistribution = "",
        [int]$InitialJitterSeconds = 0
    )
    $isMixedLoad = $LoadDurationSeconds -gt 0
    $plannedAudioSeconds = if ($isMixedLoad) {
        $Vus * ($LoadDurationSeconds + $script:mixedMaxAudioSeconds)
    }
    else { $Vus * $AudioSeconds }
    $projectedCost = ($script:attemptedAudioSeconds + $plannedAudioSeconds) * $pricePerAudioSecondUsd
    if ($projectedCost -gt $CostBudgetUsd) {
        throw "Projected ASR cost $projectedCost USD exceeds budget $CostBudgetUsd USD."
    }
    $script:attemptedAudioSeconds += $plannedAudioSeconds
    Set-TestConfiguration -Vus $Vus -Concurrency $Concurrency -QueueSize $QueueSize -Retries $Retries
    $containerId = $script:targetContainerId
    $startedAt = Get-Date
    $safeLabel = $Label.ToLowerInvariant()
    $loadName = "lili-$($runId.ToLowerInvariant())-$safeLabel"
    $summaryRelative = "$relativeResultDir/k6-summary-$safeLabel.json"
    $summaryPath = Join-Path $resultDir "k6-summary-$safeLabel.json"
    $k6LogPath = Join-Path $resultDir "k6-$safeLabel.log"
    $targetLogPath = Join-Path $resultDir "target-$safeLabel.log"
    $metricsPath = Join-Path $resultDir "metrics-$safeLabel.txt"
    $csvPath = Join-Path $resultDir "container-stats-$safeLabel.csv"
    $probePath = Join-Path $resultDir "network-probe-$safeLabel.jsonl"
    $probeContainerPath = "/tmp/network-probe-$safeLabel.jsonl"
    $rows = [System.Collections.Generic.List[object]]::new()
    $criticalReason = $null
    $clearObserved = $false
    $clearSeconds = $null
    $k6ExitCode = -1

    $audioDescription = if ($isMixedLoad) { "mixed=$AudioDurationDistribution, load=${LoadDurationSeconds}s" } else { "audio=${AudioSeconds}s" }
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] START ${Label}: VU=$Vus, $audioDescription, K=$Concurrency, Q=$QueueSize, retries=$Retries"
    if ($Stage -eq "asr" -or $Stage -eq "e2e") {
        $probeDuration = if ($isMixedLoad) {
            $LoadDurationSeconds + $script:mixedMaxAudioSeconds + $CooldownSeconds + 10
        }
        else { $AudioSeconds + $CooldownSeconds + 10 }
        & docker exec -d $containerId python /app/load/network-probe.py `
            --duration $probeDuration --interval 2 --timeout 3 --workers $ProbeWorkers `
            --primary-name $AsrProbeName --primary-host $AsrProbeHost --primary-path $AsrProbePath `
            --output $probeContainerPath
        if ($LASTEXITCODE -ne 0) { throw "Unable to start the in-container network probe." }
    }
    $dockerArguments = @(
        "run", "-d", "--name", $loadName,
        "-v", "${repoRoot}:/work", "-w", "/work",
        $K6Image, "run",
        "-e", "BASE_HTTP=http://host.docker.internal:9100",
        "-e", "BASE_WS=ws://host.docker.internal:9100",
        "-e", "ORIGIN=http://localhost:5173",
        "-e", "SERVICE_TOKEN=$testToken",
        "-e", "VUS=$Vus"
    )
    if ($isMixedLoad) {
        $dockerArguments += @(
            "-e", "ONE_SHOT=false",
            "-e", "DURATION=${LoadDurationSeconds}s",
            "-e", "AUDIO_DURATION_DISTRIBUTION=$AudioDurationDistribution",
            "-e", "START_JITTER_SECONDS=$InitialJitterSeconds"
        )
    }
    else {
        $dockerArguments += @(
            "-e", "ONE_SHOT=true",
            "-e", "AUDIO_SECONDS=$AudioSeconds"
        )
    }
    $dockerArguments += @(
        "-e", "AUDIO_FILE=/work/$AudioFile",
        "--summary-export=/work/$summaryRelative",
        "load/k6-websocket.js"
    )

    try {
        $loadId = (& docker @dockerArguments).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $loadId) { throw "Unable to start k6 for $Label." }
        while ($true) {
            $sampleStarted = Get-Date
            try {
                $sample = Get-TargetSample -ContainerId $containerId -StartedAt $startedAt
                $rows.Add($sample)
                if ($sample.container_memory_limit_percent -ge 85) { $criticalReason = "container memory reached 85%" }
                elseif ($sample.health -ne "healthy") { $criticalReason = "health changed to $($sample.health)" }
                elseif ($sample.restart_count -gt 0 -or $sample.oom_killed) { $criticalReason = "restart or OOM detected" }
            }
            catch {
                $criticalReason = "monitoring failed: $($_.Exception.Message)"
            }
            if ($criticalReason) {
                & docker stop --time 2 $loadName | Out-Null
                break
            }
            $loadInspection = @(& docker inspect $loadName | ConvertFrom-Json)[0]
            if (-not $loadInspection.State.Running) { break }
            $remaining = ($SampleIntervalSeconds * 1000) - ((Get-Date) - $sampleStarted).TotalMilliseconds
            if ($remaining -gt 0) { Start-Sleep -Milliseconds ([int][math]::Ceiling($remaining)) }
        }

        $loadInspection = @(& docker inspect $loadName | ConvertFrom-Json)[0]
        $k6ExitCode = [int]$loadInspection.State.ExitCode
        $oldPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $k6Lines = & docker logs $loadName 2>&1
        $ErrorActionPreference = $oldPreference
        $k6Lines | Set-Content -LiteralPath $k6LogPath -Encoding UTF8

        $cooldownStarted = Get-Date
        $clearSamples = 0
        while (((Get-Date) - $cooldownStarted).TotalSeconds -le $CooldownSeconds) {
            try {
                $sample = Get-TargetSample -ContainerId $containerId -StartedAt $startedAt
                $rows.Add($sample)
            }
            catch {
                if (-not $criticalReason) { $criticalReason = "cooldown monitoring failed: $($_.Exception.Message)" }
                break
            }
            if (Test-AllWorkCleared $sample) {
                $clearSamples += 1
                if ($clearSamples -ge 2) {
                    $clearObserved = $true
                    $clearSeconds = [math]::Round(((Get-Date) - $cooldownStarted).TotalSeconds, 1)
                    break
                }
            }
            else { $clearSamples = 0 }
        }
    }
    finally {
        $rows | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8
        Remove-LoadContainer $loadName
        Save-TargetLogs -ContainerId $containerId -Path $targetLogPath
        if ($Stage -eq "asr" -or $Stage -eq "e2e") {
            & docker cp "${containerId}:${probeContainerPath}" $probePath 2>$null
        }
    }

    $metrics = ""
    try { $metrics = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://127.0.0.1:9100/metrics").Content } catch {}
    $metrics | Set-Content -LiteralPath $metricsPath -Encoding UTF8
    $statusCounts = Get-AsrStatusCounts $metrics
    $polishStatusCounts = Get-PolishStatusCounts $metrics
    $summary = $null
    if (Test-Path -LiteralPath $summaryPath) { $summary = Get-Content -Raw -LiteralPath $summaryPath | ConvertFrom-Json }
    $missingLatency = 1000000000.0
    $ready = if ($summary) { [int](Get-SummaryMetric $summary "voice_ready_events" "count" 0) } else { 0 }
    $finals = if ($summary) { [int](Get-SummaryMetric $summary "voice_non_empty_finals" "count" 0) } else { 0 }
    $errors = if ($summary) { [int](Get-SummaryMetric $summary "voice_server_errors" "count" 0) } else { 0 }
    $degraded = if ($summary) { [int](Get-SummaryMetric $summary "voice_degraded_finals" "count" 0) } else { 0 }
    $requestedAudioSeconds = if ($summary) { [double](Get-SummaryMetric $summary "voice_requested_audio_seconds" "count" $plannedAudioSeconds) } else { $plannedAudioSeconds }
    $checkFails = if ($summary) { [int](Get-SummaryMetric $summary "checks" "fails" 0) } else { $Vus * 2 }
    $finalP95 = if ($summary) { [double](Get-SummaryMetric $summary "voice_stop_to_final_ms" "p(95)" $missingLatency) } else { $missingLatency }
    $finalMax = if ($summary) { [double](Get-SummaryMetric $summary "voice_stop_to_final_ms" "max" $missingLatency) } else { $missingLatency }
    $queueWaitP95 = if ($summary) { [double](Get-SummaryMetric $summary "voice_asr_queue_wait_ms" "p(95)" $missingLatency) } else { $missingLatency }
    $cpuP95 = Get-Percentile95 @($rows | ForEach-Object { $_.cpu_two_core_percent })
    $cpuMax = Get-Maximum @($rows | ForEach-Object { $_.cpu_two_core_percent })
    $memoryMax = Get-Maximum @($rows | ForEach-Object { $_.container_memory_bytes })
    $hostMemoryMax = Get-Maximum @($rows | ForEach-Object { $_.host_memory_percent })
    $activeMax = Get-Maximum @($rows | ForEach-Object { $_.active_sessions })
    $inflightMax = Get-Maximum @($rows | ForEach-Object { $_.asr_inflight })
    $queueMax = Get-Maximum @($rows | ForEach-Object { $_.asr_queue })
    $polishInflightMax = Get-Maximum @($rows | ForEach-Object { $_.polish_inflight })
    $polishQueueMax = Get-Maximum @($rows | ForEach-Object { $_.polish_queue })
    $network = Get-NetworkSummary @($rows)
    $networkProbe = Get-NetworkProbeSummary -Path $probePath
    $failures = [System.Collections.Generic.List[string]]::new()
    if ($criticalReason) { $failures.Add("critical: $criticalReason") }
    if ($k6ExitCode -ne 0) { $failures.Add("k6 exit code $k6ExitCode") }
    if ($isMixedLoad) {
        if ($ready -lt $Vus) { $failures.Add("mixed-load ready count $ready below VU $Vus") }
        if ($finals -ne $ready) { $failures.Add("mixed-load final $finals/$ready ready sessions") }
    }
    else {
        if ($ready -ne $Vus) { $failures.Add("ready $ready/$Vus") }
        if ($finals -ne $Vus) { $failures.Add("final $finals/$Vus") }
    }
    if ($errors -ne 0) { $failures.Add("server errors $errors") }
    if ($degraded -ne 0) { $failures.Add("degraded finals $degraded") }
    if ($checkFails -ne 0) { $failures.Add("check failures $checkFails") }
    if ($finalP95 -ge 10000) { $failures.Add("stop-to-final P95 $([math]::Round($finalP95, 1)) ms") }
    if ($finalMax -ge 20000) { $failures.Add("stop-to-final max $([math]::Round($finalMax, 1)) ms") }
    if ($queueWaitP95 -ge 2000) { $failures.Add("ASR queue-wait P95 $([math]::Round($queueWaitP95, 1)) ms") }
    if ($cpuP95 -ge 70) { $failures.Add("two-core CPU P95 $([math]::Round($cpuP95, 2))%") }
    if ($memoryMax -ge 2GB) { $failures.Add("container memory $([math]::Round($memoryMax / 1MB, 1)) MiB") }
    if ($Stage -eq "asr") {
        if ($inflightMax -lt [math]::Min($Concurrency, $Vus)) { $failures.Add("observed ASR inflight peak $inflightMax below K=$Concurrency") }
        if ($queueMax -ge $QueueSize) { $failures.Add("ASR queue reached limit $QueueSize") }
    }
    elseif ($Stage -eq "polish") {
        if ($polishInflightMax -lt [math]::Min($Concurrency, $Vus)) { $failures.Add("observed polish inflight peak $polishInflightMax below K=$Concurrency") }
        $expectedApplied = $Vus
        $applied = if ($polishStatusCounts.ContainsKey("applied")) { $polishStatusCounts["applied"] } else { 0 }
        if ($applied -ne $expectedApplied) { $failures.Add("polish applied $applied/$expectedApplied") }
    }
    else {
        $expectedAsr = if ($AsrConcurrency -gt 0) { [math]::Min($AsrConcurrency, $Vus) } else { [math]::Min($Concurrency, $Vus) }
        $expectedPolish = if ($PolishConcurrency -gt 0) { [math]::Min($PolishConcurrency, $Vus) } else { [math]::Min($Concurrency, $Vus) }
        if (-not $isMixedLoad) {
            if ($inflightMax -lt $expectedAsr) { $failures.Add("observed ASR inflight peak $inflightMax below expected $expectedAsr") }
            if ($polishInflightMax -lt $expectedPolish) { $failures.Add("observed polish inflight peak $polishInflightMax below expected $expectedPolish") }
        }
    }
    if (-not $clearObserved) { $failures.Add("work did not clear in $CooldownSeconds seconds") }
    foreach ($status in @("rate_limited", "provider_error", "timeout", "configuration_error", "request_error", "queue_timeout", "queue_full")) {
        if ($statusCounts.ContainsKey($status) -and $statusCounts[$status] -gt 0) { $failures.Add("ASR $status=$($statusCounts[$status])") }
    }
    foreach ($status in $polishStatusCounts.Keys) {
        if ($status -ne "applied" -and $polishStatusCounts[$status] -gt 0) { $failures.Add("polish $status=$($polishStatusCounts[$status])") }
    }
    if (@($rows | Where-Object { $_.health -ne "healthy" -or $_.restart_count -gt 0 -or $_.oom_killed }).Count -gt 0) {
        $failures.Add("health/restart/OOM invariant failed")
    }
    $providerFailure = $false
    foreach ($status in @("rate_limited", "provider_error", "timeout")) {
        if ($statusCounts.ContainsKey($status) -and $statusCounts[$status] -gt 0) { $providerFailure = $true }
        if ($polishStatusCounts.ContainsKey($status) -and $polishStatusCounts[$status] -gt 0) { $providerFailure = $true }
    }
    $result = [pscustomobject]@{
        label = $Label
        vus = $Vus
        audio_seconds = if ($isMixedLoad) { $null } else { $AudioSeconds }
        load_duration_seconds = if ($isMixedLoad) { $LoadDurationSeconds } else { $null }
        audio_duration_distribution = if ($isMixedLoad) { $AudioDurationDistribution } else { $null }
        requested_audio_seconds = [math]::Round($requestedAudioSeconds, 1)
        concurrency = $Concurrency
        queue_size = $QueueSize
        retries = $Retries
        passed = ($failures.Count -eq 0)
        critical = [bool]$criticalReason
        provider_failure = $providerFailure
        ready = $ready
        finals = $finals
        degraded_finals = $degraded
        server_errors = $errors
        final_p95_ms = [math]::Round($finalP95, 1)
        final_max_ms = [math]::Round($finalMax, 1)
        asr_queue_wait_p95_ms = [math]::Round($queueWaitP95, 1)
        asr_success = if ($statusCounts.ContainsKey("success")) { $statusCounts["success"] } else { 0 }
        asr_status_counts = $statusCounts
        polish_status_counts = $polishStatusCounts
        asr_inflight_max = $inflightMax
        asr_queue_max = $queueMax
        polish_inflight_max = $polishInflightMax
        polish_queue_max = $polishQueueMax
        active_sessions_max = $activeMax
        cpu_p95_percent = [math]::Round($cpuP95, 3)
        cpu_max_percent = [math]::Round($cpuMax, 3)
        container_memory_max_mib = [math]::Round($memoryMax / 1MB, 1)
        host_memory_max_percent = [math]::Round($hostMemoryMax, 3)
        network = $network
        network_probe = $networkProbe
        clear_seconds = $clearSeconds
        estimated_cost_usd = [math]::Round(($requestedAudioSeconds * $pricePerAudioSecondUsd), 6)
        failure_reasons = @($failures)
    }
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $resultDir "result-$safeLabel.json") -Encoding UTF8
    $status = if ($result.passed) { "PASS" } else { "FAIL" }
    $finalDenominator = if ($isMixedLoad) { $ready } else { $Vus }
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $Label ${status}: final=$finals/$finalDenominator, degraded=$degraded, final_p95=$($result.final_p95_ms)ms, queue_p95=$($result.asr_queue_wait_p95_ms)ms, inflight_max=$inflightMax, queue_max=$queueMax"
    if (-not $result.passed) { Write-Host "Reasons: $($failures -join '; ')" }
    return $result
}

function Get-SafeConcurrency {
    param([int]$HighestPassed)
    if ($HighestPassed -ge 16) { return 12 }
    if ($HighestPassed -ge 12) { return 8 }
    if ($HighestPassed -ge 8) { return 5 }
    return 3
}

function Write-FinalReport {
    param([int]$HighestPassed, [Nullable[int]]$FirstFailed, [Nullable[int]]$SafeConcurrency, [string]$RestoreResult, [string]$ErrorText)
    $report = [Text.StringBuilder]::new()
    [void]$report.AppendLine("# Real $Stage concurrency ladder result")
    [void]$report.AppendLine()
    [void]$report.AppendLine("- Run ID: $runId")
    [void]$report.AppendLine("- Highest passing short tier: $HighestPassed VU")
    if ($null -ne $FirstFailed) { [void]$report.AppendLine("- First failed short tier: $FirstFailed VU") }
    else { [void]$report.AppendLine("- First failed short tier: not found through $HighestPassed VU") }
    if ($null -ne $SafeConcurrency) { [void]$report.AppendLine("- Sustained validation concurrency Ksafe: $SafeConcurrency") }
    if ($Stage -ne "polish") {
        $estimatedCharge = [math]::Round($attemptedAudioSeconds * $pricePerAudioSecondUsd, 6)
        [void]$report.AppendLine("- Estimated maximum ASR charge: `$$estimatedCharge")
    }
    [void]$report.AppendLine("- Normal configuration restore: $RestoreResult")
    if ($ErrorText) { [void]$report.AppendLine("- Automation error: $ErrorText") }
    [void]$report.AppendLine()
    [void]$report.AppendLine("| Attempt | Result | Final | Degraded | Final P95 | Queue-wait P95 | ASR inflight/queue max | CPU P95 | Memory max | RX/TX P95 | Recommended link |")
    [void]$report.AppendLine("| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |")
    foreach ($item in $results) {
        $status = if ($item.passed) { "PASS" } else { "FAIL" }
        $finalDenominator = if ($null -ne $item.load_duration_seconds) { $item.ready } else { $item.vus }
        [void]$report.AppendLine("| $($item.label) | $status | $($item.finals)/$finalDenominator | $($item.degraded_finals) | $($item.final_p95_ms) ms | $($item.asr_queue_wait_p95_ms) ms | $($item.asr_inflight_max)/$($item.asr_queue_max) | $($item.cpu_p95_percent)% | $($item.container_memory_max_mib) MiB | $($item.network.rx_p95_mbps)/$($item.network.tx_p95_mbps) Mbps | $($item.network.recommended_mbps) Mbps |")
    }
    [void]$report.AppendLine()
    [void]$report.AppendLine("## Failure reasons")
    [void]$report.AppendLine()
    $failed = @($results | Where-Object { -not $_.passed })
    if ($failed.Count -eq 0) { [void]$report.AppendLine("No attempt failed.") }
    else { foreach ($item in $failed) { [void]$report.AppendLine("- $($item.label): $($item.failure_reasons -join '; ')") } }
    $report.ToString() | Set-Content -LiteralPath (Join-Path $resultDir "result.md") -Encoding UTF8
}

$highestPassed = 0
$firstFailed = $null
$safeConcurrency = $null

try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker CLI is unavailable." }
    if (-not (Test-Path -LiteralPath $audioPath)) { throw "Audio fixture is missing: $audioPath" }
    if ((Get-Item -LiteralPath $audioPath).Length -ne 960000) { throw "Audio fixture must be the verified 30-second PCM16 file (960000 bytes)." }
    $env:LOAD_SERVICE_TOKEN = $testToken
    Invoke-DockerChecked @("compose", "-f", $baseCompose, "-f", $testCompose, "config", "--quiet")
    $metadata = [ordered]@{
        run_id = $runId
        started_at = Get-Date -Format "o"
        tiers = $Tiers
        short_audio_seconds = $ShortAudioSeconds
        sustained_audio_seconds = $SustainedAudioSeconds
        sustained_retries = $SustainedRetries
        segment_target_seconds = if ($SegmentTargetSeconds -gt 0) { $SegmentTargetSeconds } else { 30 }
        segment_max_seconds = if ($SegmentMaxSeconds -gt 0) { $SegmentMaxSeconds } else { 45 }
        mixed_load_minutes = $MixedLoadMinutes
        mixed_rounds = $MixedRounds
        sustained_rounds = $SustainedRounds
        mixed_audio_duration_distribution = if ($MixedLoadMinutes -gt 0) { $MixedAudioDurationDistribution } else { $null }
        start_jitter_seconds = if ($MixedLoadMinutes -gt 0) { $StartJitterSeconds } else { $null }
        fixture = $AudioFile
        fixture_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $audioPath).Hash
        stage = $Stage
        provider = if ($Stage -eq "asr") { $ProviderName } elseif ($Stage -eq "polish") { "DMX" } else { "$ProviderName+DMX" }
        asr_model = $AsrModel
        polish_model = if ($Stage -eq "asr") { $null } else { "deepseek-v4-flash-0731" }
        polish_enabled = ($Stage -ne "asr")
        redis_enabled = $false
        uvicorn_workers = 1
        price_per_audio_second_usd = if ($Stage -eq "polish") { 0 } else { $pricePerAudioSecondUsd }
        cost_budget_usd = $CostBudgetUsd
        credentials_recorded = $false
    }
    $metadata | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $resultDir "metadata.json") -Encoding UTF8
    Write-Host "Evidence directory: $resultDir"

    foreach ($tier in $Tiers) {
        $queueSize = $tier * 4
        $attempt = Invoke-LoadAttempt -Vus $tier -AudioSeconds $ShortAudioSeconds -Concurrency $tier -QueueSize $queueSize -Retries 0 -Label "short-${tier}vu"
        $results.Add($attempt)
        if ($attempt.passed) {
            $highestPassed = $tier
            continue
        }
        if ($attempt.critical) { $firstFailed = $tier; break }
        if ($attempt.provider_failure) {
            Write-Host "Provider-only failure detected. Cooling down 120 seconds before one retry."
            foreach ($remaining in @(120, 90, 60, 30)) {
                Write-Host "Retry cooldown: $remaining seconds remaining."
                Start-Sleep -Seconds 30
            }
            $retry = Invoke-LoadAttempt -Vus $tier -AudioSeconds $ShortAudioSeconds -Concurrency $tier -QueueSize $queueSize -Retries 0 -Label "short-${tier}vu-retry"
            $results.Add($retry)
            if ($retry.passed) {
                $highestPassed = $tier
                continue
            }
        }
        $firstFailed = $tier
        break
    }

    if ($null -ne $firstFailed) {
        Write-Host "Skipping sustained validation because short tier $firstFailed failed."
    }
    elseif ($SustainedOnlyConcurrency -gt 0) {
        $safeConcurrency = $SustainedOnlyConcurrency
        if ($MixedLoadMinutes -gt 0) {
            $mixedQueueSize = if ($AsrQueueSize -gt 0) { $AsrQueueSize } else { $safeConcurrency * 2 }
            foreach ($round in 1..$MixedRounds) {
                $mixed = Invoke-LoadAttempt -Vus $safeConcurrency -Concurrency $safeConcurrency -QueueSize $mixedQueueSize -Retries $SustainedRetries -Label "mixed-${safeConcurrency}vu-r${round}" -LoadDurationSeconds ($MixedLoadMinutes * 60) -AudioDurationDistribution $MixedAudioDurationDistribution -InitialJitterSeconds $StartJitterSeconds
                $results.Add($mixed)
            }
        }
        else {
            foreach ($round in 1..$SustainedRounds) {
                $label = if ($SustainedRounds -gt 1) { "sustained-${safeConcurrency}vu-r${round}" } else { "sustained-${safeConcurrency}vu" }
                $sustained = Invoke-LoadAttempt -Vus $safeConcurrency -AudioSeconds $SustainedAudioSeconds -Concurrency $safeConcurrency -QueueSize ($safeConcurrency * 2) -Retries $SustainedRetries -Label $label
                $results.Add($sustained)
            }
        }
    }
    elseif ($highestPassed -gt 0) {
        $safeConcurrency = Get-SafeConcurrency $highestPassed
        foreach ($round in 1..$SustainedRounds) {
            $label = if ($SustainedRounds -gt 1) { "sustained-${safeConcurrency}vu-r${round}" } else { "sustained-${safeConcurrency}vu" }
            $sustained = Invoke-LoadAttempt -Vus $safeConcurrency -AudioSeconds $SustainedAudioSeconds -Concurrency $safeConcurrency -QueueSize ($safeConcurrency * 2) -Retries $SustainedRetries -Label $label
            $results.Add($sustained)
        }
    }
}
catch {
    $fatalError = "$($_.Exception.Message) (line $($_.InvocationInfo.ScriptLineNumber))"
    Write-Warning $fatalError
}
finally {
    try {
        Save-TargetLogs -ContainerId $targetContainerId -Path (Join-Path $resultDir "target-last.log")
        foreach ($name in $savedEnvironment.Keys) {
            if ($null -eq $savedEnvironment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
            else { [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process") }
        }
        Invoke-DockerChecked @("compose", "-f", $baseCompose, "up", "-d", "--force-recreate", $serviceName)
        $normalId = (& docker compose -f $baseCompose ps -q $serviceName).Trim()
        Wait-TargetHealthy -ContainerId $normalId
        $normal = @(& docker inspect $normalId | ConvertFrom-Json)[0]
        $normalEnvironment = @($normal.Config.Env)
        if ($normalEnvironment -contains "SERVICE_TOKEN=$testToken") {
            throw "Temporary capacity-test settings remained active after restore."
        }
        $restoreStatus = "successful; service healthy; temporary capacity-test overrides removed"
    }
    catch {
        $restoreStatus = "failed: $($_.Exception.Message)"
        if (-not $fatalError) { $fatalError = "normal configuration restore failed" }
    }
    Write-FinalReport -HighestPassed $highestPassed -FirstFailed $firstFailed -SafeConcurrency $safeConcurrency -RestoreResult $restoreStatus -ErrorText $fatalError
    $decision = [ordered]@{
        run_id = $runId
        highest_passing_short_tier = $highestPassed
        first_failed_short_tier = $firstFailed
        safe_concurrency = $safeConcurrency
        sustained_passed = [bool](
            @($results | Where-Object { $_.label -like "sustained-*" -or $_.label -like "mixed-*" }).Count -gt 0 -and
            @($results | Where-Object { ($_.label -like "sustained-*" -or $_.label -like "mixed-*") -and -not $_.passed }).Count -eq 0
        )
        attempted_audio_seconds = $attemptedAudioSeconds
        estimated_cost_usd = [math]::Round($attemptedAudioSeconds * $pricePerAudioSecondUsd, 6)
        budget_usd = $CostBudgetUsd
        restore_status = $restoreStatus
        fatal_error = $fatalError
        attempts = $results
    }
    $decision | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $resultDir "decision.json") -Encoding UTF8
    Write-Host "Result: $resultDir"
    Write-Host "Restore: $restoreStatus"
}

if ($fatalError) { exit 2 }
if (@($results | Where-Object { -not $_.passed }).Count -gt 0) { exit 1 }
exit 0
