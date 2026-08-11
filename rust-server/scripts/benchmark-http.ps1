param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$Bundle,
    [Parameter(Mandatory = $true)][string]$LegacyRoot,
    [Parameter(Mandatory = $true)][string]$Corpus,
    [Parameter(Mandatory = $true)][string]$Output,
    [int]$PortBase = 51520,
    [int]$MixedRequests = 4096,
    [int]$HotRequests = 1000,
    [int]$ConcurrentRequests = 2048,
    [int]$Concurrency = 32,
    [int]$AudioRequests = 200,
    [int]$Repeats = 3
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http

function Get-Percentile {
    param([double[]]$Values, [double]$Quantile)
    $sorted = @($Values | Sort-Object)
    $index = [math]::Round(($sorted.Count - 1) * $Quantile)
    return $sorted[$index]
}

function Invoke-Bytes {
    param([System.Net.Http.HttpClient]$Client, [string]$Url)
    return $Client.GetByteArrayAsync($Url).GetAwaiter().GetResult()
}

function Get-QueryPath {
    param($Query)
    $path = "/?term=" + [Uri]::EscapeDataString([string]$Query.term)
    if ($null -ne $Query.reading) {
        $path += "&reading=" + [Uri]::EscapeDataString([string]$Query.reading)
    }
    if ($null -ne $Query.sources) {
        $encoded = @($Query.sources | ForEach-Object { [Uri]::EscapeDataString([string]$_) })
        $path += "&sources=" + ($encoded -join ",")
    }
    if (@($Query.users).Count -gt 0) {
        $encoded = @($Query.users | ForEach-Object { [Uri]::EscapeDataString([string]$_) })
        $path += "&user=" + ($encoded -join ",")
    }
    return $path
}

function Measure-Sequential {
    param(
        [System.Net.Http.HttpClient]$Client,
        [string]$BaseUrl,
        [string[]]$Paths,
        [int]$Count
    )
    [double[]]$latency = [double[]]::new($Count)
    $bytes = 0L
    for ($request = 0; $request -lt $Count; $request++) {
        $timer = [System.Diagnostics.Stopwatch]::StartNew()
        $body = Invoke-Bytes $Client ($BaseUrl + $Paths[$request % $Paths.Count])
        $timer.Stop()
        $latency[$request] = $timer.Elapsed.TotalMilliseconds * 1000.0
        $bytes += $body.Length
    }
    return [pscustomobject]@{
        requests = $Count
        responseBytes = $bytes
        medianMicroseconds = Get-Percentile $latency 0.50
        p95Microseconds = Get-Percentile $latency 0.95
        p99Microseconds = Get-Percentile $latency 0.99
    }
}

$exePath = (Resolve-Path -LiteralPath $Exe).Path
$bundlePath = (Resolve-Path -LiteralPath $Bundle).Path
$legacyPath = (Resolve-Path -LiteralPath $LegacyRoot).Path
$corpusPath = (Resolve-Path -LiteralPath $Corpus).Path
$corpusText = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($corpusPath))
$corpusItems = ConvertFrom-Json -InputObject $corpusText
if ($corpusItems.Count -lt 32) {
    throw "Corpus must contain at least 32 queries"
}
[string[]]$queryPaths = @($corpusItems | ForEach-Object { Get-QueryPath $_ })
$outputPath = [System.IO.Path]::GetFullPath($Output)
$outputParent = [System.IO.Path]::GetDirectoryName($outputPath)
[System.IO.Directory]::CreateDirectory($outputParent) | Out-Null
$runRoot = Join-Path $outputParent ("http-bench-" + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
[System.IO.Directory]::CreateDirectory($runRoot) | Out-Null

$variants = @(
    [pscustomobject]@{ Name = "mmap-sorted_pack"; Lookup = "sorted"; Assets = "pack" },
    [pscustomobject]@{ Name = "mmap-chd_pack"; Lookup = "mph"; Assets = "pack" },
    [pscustomobject]@{ Name = "preload-hashmap_pack"; Lookup = "preload"; Assets = "pack" },
    [pscustomobject]@{ Name = "mmap-sorted_individual-files"; Lookup = "sorted"; Assets = "files" }
)

$results = @()
$runIndex = 0
for ($repeat = 1; $repeat -le $Repeats; $repeat++) {
    foreach ($variant in $variants) {
        $port = $PortBase + $runIndex
        $runIndex += 1
        $baseUrl = "http://127.0.0.1:$port"
        $runName = $variant.Name + "_r" + $repeat
        $stdout = Join-Path $runRoot ($runName + ".stdout.log")
        $stderr = Join-Path $runRoot ($runName + ".stderr.log")
        $arguments = @(
            "serve", "--bundle", $bundlePath,
            "--host", "127.0.0.1", "--port", "$port",
            "--lookup-mode", $variant.Lookup,
            "--asset-mode", $variant.Assets,
            "--response-cache-entries", "0"
        )
        if ($variant.Assets -eq "files") {
            $arguments += @("--legacy-root", $legacyPath)
        }

        $startup = [System.Diagnostics.Stopwatch]::StartNew()
        $process = Start-Process -FilePath $exePath -ArgumentList $arguments -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        try {
            $handler = [System.Net.Http.HttpClientHandler]::new()
            $handler.MaxConnectionsPerServer = $Concurrency
            $client = [System.Net.Http.HttpClient]::new($handler)
            $client.Timeout = [TimeSpan]::FromSeconds(10)
            $ready = $false
            for ($attempt = 0; $attempt -lt 4000; $attempt++) {
                if ($process.HasExited) {
                    throw "Server exited during startup. See $stderr"
                }
                try {
                    $response = $client.GetAsync("$baseUrl/healthz").GetAwaiter().GetResult()
                    if ($response.IsSuccessStatusCode) {
                        $ready = $true
                        break
                    }
                } catch {
                    # A connection refusal is expected until the listener is ready.
                }
                Start-Sleep -Milliseconds 5
            }
            if (-not $ready) {
                throw "Timed out waiting for $baseUrl/healthz"
            }
            $startup.Stop()
            $process.Refresh()
            $startupWorkingSet = $process.WorkingSet64
            $startupPrivate = $process.PrivateMemorySize64

            $hotPath = $null
            $audioUrl = $null
            foreach ($path in $queryPaths) {
                $body = Invoke-Bytes $client ($baseUrl + $path)
                $parsed = [System.Text.Encoding]::UTF8.GetString($body) | ConvertFrom-Json
                if ($parsed.audioSources.Count -gt 0) {
                    $hotPath = $path
                    $audioUrl = [string]$parsed.audioSources[0].url
                    break
                }
            }
            if ($null -eq $hotPath) {
                throw "Corpus contains no hit"
            }

            for ($warm = 0; $warm -lt [math]::Min(256, $queryPaths.Count); $warm++) {
                [void](Invoke-Bytes $client ($baseUrl + $queryPaths[$warm]))
            }
            $mixed = Measure-Sequential $client $baseUrl $queryPaths $MixedRequests
            for ($warm = 0; $warm -lt 100; $warm++) {
                [void](Invoke-Bytes $client ($baseUrl + $hotPath))
            }
            $hot = Measure-Sequential $client $baseUrl @($hotPath) $HotRequests

            $concurrentTimer = [System.Diagnostics.Stopwatch]::StartNew()
            $issued = 0
            while ($issued -lt $ConcurrentRequests) {
                $batch = [math]::Min($Concurrency, $ConcurrentRequests - $issued)
                $tasks = @()
                for ($index = 0; $index -lt $batch; $index++) {
                    $path = $queryPaths[($issued + $index) % $queryPaths.Count]
                    $tasks += $client.GetByteArrayAsync($baseUrl + $path)
                }
                foreach ($task in $tasks) {
                    [void]$task.GetAwaiter().GetResult()
                }
                $issued += $batch
            }
            $concurrentTimer.Stop()

            [void](Invoke-Bytes $client $audioUrl)
            [double[]]$audioMicroseconds = [double[]]::new($AudioRequests)
            $audioBytes = 0L
            for ($request = 0; $request -lt $AudioRequests; $request++) {
                $timer = [System.Diagnostics.Stopwatch]::StartNew()
                $bytes = Invoke-Bytes $client $audioUrl
                $timer.Stop()
                $audioMicroseconds[$request] = $timer.Elapsed.TotalMilliseconds * 1000.0
                $audioBytes += $bytes.Length
            }

            $headRequest = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Head, $audioUrl)
            $headResponse = $client.SendAsync($headRequest).GetAwaiter().GetResult()
            $rangeRequest = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $audioUrl)
            $rangeRequest.Headers.Range = [System.Net.Http.Headers.RangeHeaderValue]::new(0, 15)
            $rangeResponse = $client.SendAsync($rangeRequest).GetAwaiter().GetResult()
            $rangeBytes = $rangeResponse.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            $process.Refresh()

            $results += [pscustomobject]@{
                name = $variant.Name
                repeat = $repeat
                port = $port
                lookupMode = $variant.Lookup
                assetMode = $variant.Assets
                startupMilliseconds = $startup.Elapsed.TotalMilliseconds
                startupWorkingSetBytes = $startupWorkingSet
                startupPrivateBytes = $startupPrivate
                warmedWorkingSetBytes = $process.WorkingSet64
                warmedPrivateBytes = $process.PrivateMemorySize64
                mixedLookup = $mixed
                hotLookup = $hot
                concurrency = [pscustomobject]@{
                    requests = $ConcurrentRequests
                    concurrency = $Concurrency
                    totalMilliseconds = $concurrentTimer.Elapsed.TotalMilliseconds
                    requestsPerSecond = $ConcurrentRequests / $concurrentTimer.Elapsed.TotalSeconds
                }
                audio = [pscustomobject]@{
                    requests = $AudioRequests
                    totalBytes = $audioBytes
                    medianMicroseconds = Get-Percentile $audioMicroseconds 0.50
                    p95Microseconds = Get-Percentile $audioMicroseconds 0.95
                    p99Microseconds = Get-Percentile $audioMicroseconds 0.99
                }
                protocol = [pscustomobject]@{
                    headStatus = [int]$headResponse.StatusCode
                    acceptRanges = [string]$headResponse.Headers.AcceptRanges
                    etag = [string]$headResponse.Headers.ETag
                    rangeStatus = [int]$rangeResponse.StatusCode
                    rangeBytes = $rangeBytes.Length
                    contentRange = [string]$rangeResponse.Content.Headers.ContentRange
                    cors = [string]($rangeResponse.Headers.GetValues("Access-Control-Allow-Origin") -join ",")
                }
                stdoutLog = $stdout
                stderrLog = $stderr
            }
            $client.Dispose()
            $handler.Dispose()
        } finally {
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id
                $process.WaitForExit()
            }
        }
    }
}

$report = [pscustomobject]@{
    generatedUtc = [DateTimeOffset]::UtcNow.ToString("o")
    machine = [Environment]::MachineName
    executable = $exePath
    executableSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $exePath).Hash
    corpus = $corpusPath
    corpusSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $corpusPath).Hash
    corpusQueries = $queryPaths.Count
    responseCacheEntries = 0
    mixedRequestsPerRun = $MixedRequests
    hotRequestsPerRun = $HotRequests
    concurrentRequestsPerRun = $ConcurrentRequests
    concurrency = $Concurrency
    audioRequestsPerRun = $AudioRequests
    repeats = $Repeats
    runs = $results
}
$report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $outputPath -Encoding utf8
$report | ConvertTo-Json -Depth 10
