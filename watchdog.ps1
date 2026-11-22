# XiaoAi Chat watchdog.
# Scheduled every 5 minutes by Windows Task Scheduler (task name: XiaoAiChatWatchdog).
# If main.py is not running, start it again.

$ErrorActionPreference = 'SilentlyContinue'

$ProjectDir = 'D:\workspace-wuyc\xiaoai-chat'
$Entry      = Join-Path $ProjectDir 'main.py'
$Python     = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
$LogPath    = Join-Path $ProjectDir 'watchdog.log'
$NotifyPort = 8787

# Only one watchdog instance at a time.
$mutex = New-Object System.Threading.Mutex($false, 'Local\XiaoAiChatWatchdog')
if (-not $mutex.WaitOne(1000)) { exit 0 }

try {
    function Write-WdLog([string]$Message) {
        $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    }

    # Rotate the log so it cannot grow forever.
    if ((Test-Path -LiteralPath $LogPath) -and ((Get-Item -LiteralPath $LogPath).Length -gt 2MB)) {
        Move-Item -LiteralPath $LogPath -Destination ($LogPath + '.1') -Force
    }

    $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
               Where-Object { $_.CommandLine -match 'main\.py' })

    if ($procs.Count -gt 0) {
        $ids = ($procs | ForEach-Object { $_.ProcessId }) -join ','
        Write-WdLog ("OK pid={0}" -f $ids)
        exit 0
    }

    $portUp = [bool](Get-NetTCPConnection -LocalPort $NotifyPort -State Listen -ErrorAction SilentlyContinue)
    if ($portUp) {
        Write-WdLog ("WARN main.py not found but port {0} is busy; skip start" -f $NotifyPort)
        exit 0
    }

    if (-not (Test-Path -LiteralPath $Python)) {
        Write-WdLog ("ERROR python not found: {0}" -f $Python)
        exit 1
    }

    # NO_PROXY containing [::1] makes httpx2 fail with 'Invalid port'; drop it for the child.
    Remove-Item Env:NO_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:no_proxy -ErrorAction SilentlyContinue

    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $outLog = Join-Path $ProjectDir ("watchdog-run-$stamp.out")
    $errLog = Join-Path $ProjectDir ("watchdog-run-$stamp.err")

    Start-Process -FilePath $Python -ArgumentList ('"{0}"' -f $Entry) `
        -WorkingDirectory $ProjectDir -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog | Out-Null

    $started = @()
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        $started = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
                     Where-Object { $_.CommandLine -match 'main\.py' })
        if ($started.Count -gt 0) { break }
    }

    if ($started.Count -gt 0) {
        $ids = ($started | ForEach-Object { $_.ProcessId }) -join ','
        Write-WdLog ("DOWN -> restarted, pid={0}, stdout={1}" -f $ids, $outLog)
    } else {
        Write-WdLog ("ERROR restart failed, see {0}" -f $outLog)
    }

    # Keep only the 5 newest run logs.
    Get-ChildItem -LiteralPath $ProjectDir -Filter 'watchdog-run-*.out' |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 | Remove-Item -Force
    Get-ChildItem -LiteralPath $ProjectDir -Filter 'watchdog-run-*.err' |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 | Remove-Item -Force
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}