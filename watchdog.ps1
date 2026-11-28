# XiaoAi Chat watchdog v2
# Scheduled every 1 minute by Windows Task Scheduler (task name: XiaoAiChatWatchdog).
# Supervises two processes:
#   - go-music-dl (music search backend, port 8080)
#   - main.py     (Xiao Ai bridge, notify server on port 8787)
# Checks both liveness AND service health (port listening), so a process that is
# alive but wedged (e.g. hammered by code 999 rate limiting) gets restarted.

$ErrorActionPreference = 'SilentlyContinue'

$ProjectDir  = 'D:\workspace-wuyc\xiaoai-chat'
$Entry       = Join-Path $ProjectDir 'main.py'
$Python      = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
$LogPath     = Join-Path $ProjectDir 'watchdog.log'
$StatePath   = Join-Path $ProjectDir 'watchdog.state'
$XiaoAiLog   = Join-Path $ProjectDir 'xiaoai.log'
$NotifyPort  = 8787
$MusicPort   = 8080
$MusicDl     = 'D:\workspace-wuyc\go-music-dl\music-dl.exe'
$MusicDlDir  = Split-Path -Parent $MusicDl

# Nightly quiet window 23:00-07:00: keep main.py stopped so the speaker never
# resumes playback on its own while everyone sleeps.
$now   = Get-Date
$quiet = ($now.Hour -ge 23) -or ($now.Hour -lt 7)

# Concurrency is handled by the task itself (MultipleInstances = IgnoreNew).
# An in-script mutex used to exit 0 silently whenever a previous run was still
# finishing, which looked identical to a healthy run: no log line at all.

$ErrorActionPreference = 'SilentlyContinue'

function Write-WdLog([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Test-PortListen([int]$Port) {
    # Plain TcpClient connect test: Get-NetTCPConnection depends on the NetTCPIP
    # module, which is not reliably available in a Scheduled Task session and
    # silently returned false there, causing duplicate launches.
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(800) -and $client.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Clear-ProxyEnv {
    # The host session may inject HTTPS_PROXY and https_proxy at the same time,
    # which makes Start-Process throw (duplicate dictionary key). Clear the
    # lowercase aliases, and NO_PROXY (its [::1] entry breaks httpx).
    foreach ($n in @('http_proxy', 'https_proxy', 'all_proxy')) {
        try { [System.Environment]::SetEnvironmentVariable($n, $null) } catch { }
    }
    foreach ($n in @('NO_PROXY', 'no_proxy')) {
        try { [System.Environment]::SetEnvironmentVariable($n, $null) } catch { }
    }
}

function Get-RecentLogHealth([int]$WindowMinutes) {
    # Returns @{ lines = n; rate999 = k } for the recent window of xiaoai.log.
    # Pure-ASCII matching only: the log is UTF-8 and this script must stay
    # encoding-safe, so we never match Chinese text.
    $result = @{ lines = 0; rate999 = 0 }
    if (-not (Test-Path -LiteralPath $XiaoAiLog)) { return $result }
    $cut = (Get-Date).AddMinutes(-$WindowMinutes)
    $tail = Get-Content -LiteralPath $XiaoAiLog -Tail 600 -Encoding UTF8
    foreach ($line in $tail) {
        if ($line -notmatch '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') { continue }
        $stamp = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture)
        if ($stamp -lt $cut) { continue }
        $result.lines++
        if ($line -match 'code 999|code=999') { $result.rate999++ }
    }
    return $result
}

function Get-MainProcs {
    # Callers wrap this in @(): the pipeline unfolds a bare array, so returning
    # a plain array keeps "empty" and "one element" distinguishable.
    return Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -match 'main\.py' }
}

function Start-MusicDl {
    if (-not (Test-Path -LiteralPath $MusicDl)) {
        Write-WdLog ("ERROR music-dl not found: {0}" -f $MusicDl)
        return
    }
    Clear-ProxyEnv
    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $outLog = Join-Path $MusicDlDir ("music-dl-{0}.out" -f $stamp)
    $errLog = Join-Path $MusicDlDir ("music-dl-{0}.err" -f $stamp)
    Start-Process -FilePath $MusicDl -ArgumentList 'web', '--no-browser' `
        -WorkingDirectory $MusicDlDir -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog | Out-Null
    Start-Sleep -Seconds 3
    $p = @(Get-CimInstance Win32_Process -Filter "Name='music-dl.exe'")
    if ($p.Count -gt 0) {
        $ids = ($p | ForEach-Object { $_.ProcessId }) -join ','
        Write-WdLog ("music-dl DOWN -> restarted, pid={0}" -f $ids)
    } else {
        Write-WdLog ("ERROR music-dl restart failed, see {0} / {1}" -f $outLog, $errLog)
    }
    # Keep only the newest 5 launch log pairs.
    Get-ChildItem -LiteralPath $MusicDlDir -Filter 'music-dl-*.out' |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 | Remove-Item -Force
    Get-ChildItem -LiteralPath $MusicDlDir -Filter 'music-dl-*.err' |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 | Remove-Item -Force
}

function Start-Main {
    # Last line of defence against duplicate launches: never start a second copy
    # on a port that is already served.
    if (Test-PortListen $NotifyPort) {
        Write-WdLog ("skip start: notify port {0} already listening" -f $NotifyPort)
        return
    }
    Clear-ProxyEnv
    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $outLog = Join-Path $ProjectDir ("watchdog-run-$stamp.out")
    $errLog = Join-Path $ProjectDir ("watchdog-run-$stamp.err")
    Start-Process -FilePath $Python -ArgumentList ('"{0}"' -f $Entry) `
        -WorkingDirectory $ProjectDir -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog | Out-Null

    $started = @()
    # A python.exe whose CommandLine is not yet visible in WMI still counts as up
    # once the notify port answers, so wait on the port first and fall back to
    # the process list. 45s covers a cold start (token restore + history skip).
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 1
        if (Test-PortListen $NotifyPort) { break }
    }
    $started = @(Get-MainProcs)
    if ($started.Count -gt 0 -or (Test-PortListen $NotifyPort)) {
        $ids = ($started | ForEach-Object { $_.ProcessId }) -join ','
        Write-WdLog ("main.py DOWN -> restarted, pid={0}, notify={1}, stdout={2}" -f $ids, (Test-PortListen $NotifyPort), $outLog)
    } else {
        Write-WdLog ("ERROR main.py restart failed, see {0}" -f $outLog)
    }
    Get-ChildItem -LiteralPath $ProjectDir -Filter 'watchdog-run-*.out' |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 | Remove-Item -Force
    Get-ChildItem -LiteralPath $ProjectDir -Filter 'watchdog-run-*.err' |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 | Remove-Item -Force
}

try {
    # Rotate the watchdog log so it cannot grow forever.
    if ((Test-Path -LiteralPath $LogPath) -and ((Get-Item -LiteralPath $LogPath).Length -gt 2MB)) {
        Move-Item -LiteralPath $LogPath -Destination ($LogPath + '.1') -Force
    }

    # ---------- go-music-dl: search backend for online song requests ----------
    $musicProcs  = @(Get-CimInstance Win32_Process -Filter "Name='music-dl.exe'")
    $musicListen = Test-PortListen $MusicPort

    if ($musicProcs.Count -gt 0 -and -not $musicListen) {
        $ids = ($musicProcs | ForEach-Object { $_.ProcessId }) -join ','
        $musicProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Write-WdLog ("music-dl unresponsive (port {0} not listening) -> killed pid={1}" -f $MusicPort, $ids)
        Start-Sleep -Seconds 2
        Start-MusicDl
    } elseif ($musicProcs.Count -eq 0 -and -not $musicListen) {
        Start-MusicDl
    }

    # ---------- main.py ----------
    $procs = @(Get-MainProcs)

    if ($quiet) {
        if ($procs.Count -gt 0) {
            $ids = ($procs | ForEach-Object { $_.ProcessId }) -join ','
            $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
            Write-WdLog ("quiet hours -> stopped main.py pid={0}" -f $ids)
        } else {
            Write-WdLog 'quiet hours (23:00-07:00), skip start'
        }
        exit 0
    }

    # WMI sometimes hides the CommandLine of a freshly started python.exe.
    # If the notify port answers, the bridge is up regardless of the process
    # list, so never start a second copy on top of it.
    if ($procs.Count -eq 0 -and (Test-PortListen $NotifyPort)) {
        Write-WdLog ("OK (notify port {0} listening; process list not visible yet)" -f $NotifyPort)
        exit 0
    }

    if ($procs.Count -gt 0) {
        $ids  = ($procs | ForEach-Object { $_.ProcessId }) -join ','
        $up   = Test-PortListen $NotifyPort
        $young = $false
        foreach ($p in $procs) {
            $startedAt = $p.CreationDate
            if ($startedAt -and ((Get-Date) - $startedAt).TotalSeconds -lt 90) { $young = $true }
        }

        # Alive but the notify server never came up: a failed boot, not a transient.
        if (-not $up -and -not $young) {
            $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
            Write-WdLog ("main.py alive but port {0} not listening -> killed pid={1}, restarting" -f $NotifyPort, $ids)
            Start-Main
            exit 0
        }

        # Alive and serving, but drowned in code 999 rate limiting: restart at most
        # once per hour to avoid a restart loop while Xiaomi keeps throttling us.
        $health = Get-RecentLogHealth 10
        if ($health.lines -ge 100 -and ($health.rate999 / [math]::Max($health.lines, 1)) -gt 0.85) {
            $lastKill = $null
            if (Test-Path -LiteralPath $StatePath) {
                $raw = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8
                if ($raw -match 'last999kill=(\d+)') { $lastKill = [int64]$Matches[1] }
            }
            $nowUnix = [int64]([datetimeoffset]::UtcNow.ToUnixTimeSeconds())
            if (-not $lastKill -or ($nowUnix - $lastKill) -gt 3600) {
                $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
                Set-Content -LiteralPath $StatePath -Value ("last999kill={0}" -f $nowUnix) -Encoding UTF8
                Write-WdLog ("main.py wedged by code 999 ({0}/{1} lines in 10 min) -> killed pid={2}, restarting" -f $health.rate999, $health.lines, $ids)
                Start-Sleep -Seconds 2
                Start-Main
                exit 0
            }
            Write-WdLog ("WARN code 999 storm ({0}/{1} lines) but restart is cooling down" -f $health.rate999, $health.lines)
        }

        Write-WdLog ("OK pid={0} notify={1} music={2}" -f $ids, $up, (Test-PortListen $MusicPort))
        exit 0
    }

    if (-not (Test-Path -LiteralPath $Python)) {
        Write-WdLog ("ERROR python not found: {0}" -f $Python)
        exit 1
    }

    Start-Main
}
catch {
    Write-WdLog ("ERROR unhandled exception: {0}" -f $_.Exception.Message)
}
