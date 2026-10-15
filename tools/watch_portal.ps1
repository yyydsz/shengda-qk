# Availability-window monitor for the teaching-affairs portal (low-frequency, read-only)
#
# Why this exists: during course-selection rush, 443 on my.shengda.edu.cn /
# cas.shengda.edu.cn flaps between "refused" and "accepted". The open windows are
# random, so manual refreshing almost always misses them. This watches for them.
#
# Design rules (important):
#   1. TCP probe first (extremely cheap, no application-layer load). Only if the
#      TCP connect succeeds do we issue ONE HTTP GET.
#   2. Default 20s interval, no concurrency, no retry storm. The server is already
#      overloaded; hammering it only makes everyone (including you) fail harder.
#   3. Strictly read-only: GETs the login page / portal index. Never POSTs, never
#      touches course-selection endpoints.
#
# NOTE: output is deliberately ASCII-only. Windows PowerShell 5.1 reads .ps1 as
# ANSI/GBK unless the file has a UTF-8 BOM, so non-ASCII text here would corrupt
# the parser. Keep this file ASCII-only if you edit it.
#
# Usage (this box has 5.1, so use powershell.exe -- there is no pwsh 7):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\watch_portal.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\watch_portal.ps1 -Interval 30
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\watch_portal.ps1 -MaxChecks 30

[CmdletBinding()]
param(
    [int]$Interval = 20,
    [int]$MaxChecks = 0,
    [int]$TcpTimeoutMs = 4000,
    [int]$HttpTimeoutSec = 12,
    [string]$LogPath
)

$ErrorActionPreference = 'Continue'
$targets = @(
    @{ Name = 'portal'; Host = 'my.shengda.edu.cn';  Path = '/' },
    @{ Name = 'cas';    Host = 'cas.shengda.edu.cn'; Path = '/lyuapServer/loginType' }
)

if (-not $LogPath) { $LogPath = Join-Path $PSScriptRoot 'watch_portal.log' }

function Write-Log([string]$Line) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $full = "$stamp  $Line"
    Write-Host $full
    Add-Content -Path $LogPath -Value $full -Encoding UTF8
}

function Test-Tcp([string]$HostName, [int]$Port, [int]$TimeoutMs) {
    # New-Object rather than ::new() so PowerShell 5.1 can parse it
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        if ($task.Wait($TimeoutMs) -and $client.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

# Only called after TCP succeeded, so we never spam a dead port with HTTP requests.
# PS 5.1 has no -SkipHttpErrorCheck, so 4xx/5xx throw; pull the code from the exception.
function Get-HttpStatus([string]$HostName, [string]$Path, [int]$TimeoutSec) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $url = "https://$HostName$Path"
    try {
        $resp = Invoke-WebRequest -Uri $url -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop
        $sw.Stop()
        return @{ Status = [int]$resp.StatusCode; Ms = [int]$sw.ElapsedMilliseconds; Err = '' }
    } catch {
        $sw.Stop()
        $status = 0
        $err = $_.Exception.Message
        $webResp = $null
        if ($_.Exception.PSObject.Properties.Name -contains 'Response') { $webResp = $_.Exception.Response }
        if ($null -ne $webResp) {
            try { $status = [int]$webResp.StatusCode } catch { $status = 0 }
        }
        return @{ Status = $status; Ms = [int]$sw.ElapsedMilliseconds; Err = $err }
    }
}

Write-Log "monitor start | interval=${Interval}s tcp_timeout=${TcpTimeoutMs}ms log=$LogPath"
Write-Log "read-only availability probe; it does not and cannot log in or select courses for you"

$check = 0
$openWindows = 0
try {
    while ($true) {
        $check++
        $parts = @()
        $anyHttpOk = $false

        foreach ($t in $targets) {
            $tcp = Test-Tcp -HostName $t.Host -Port 443 -TimeoutMs $TcpTimeoutMs
            if (-not $tcp) {
                $parts += ("{0}=TCP_FAIL" -f $t.Name)
                continue
            }
            # Port is open, so now it is worth asking the application layer
            $http = Get-HttpStatus -HostName $t.Host -Path $t.Path -TimeoutSec $HttpTimeoutSec
            if ($http.Status -ge 200 -and $http.Status -lt 400) {
                $parts += ("{0}=OK({1},{2}ms)" -f $t.Name, $http.Status, $http.Ms)
                $anyHttpOk = $true
            } elseif ($http.Status -gt 0) {
                $parts += ("{0}=HTTP{1}" -f $t.Name, $http.Status)
            } else {
                $parts += ("{0}=TCP_OK_HTTP_DEAD" -f $t.Name)
            }
        }

        $line = ("#{0}  {1}" -f $check, ($parts -join '  '))

        if ($anyHttpOk) {
            $openWindows++
            Write-Log ("[WINDOW] " + $line)
            Write-Host ''
            Write-Host '  *** SERVICE IS REACHABLE -- refresh the login/selection page NOW ***' -ForegroundColor Black -BackgroundColor Green
            Write-Host ''
            # Audible nudge; failure here must not break monitoring
            try { 1..3 | ForEach-Object { [console]::beep(880, 180); Start-Sleep -Milliseconds 120 } } catch { }
        } else {
            Write-Log $line
        }

        if ($MaxChecks -gt 0 -and $check -ge $MaxChecks) { break }
        Start-Sleep -Seconds $Interval
    }
} finally {
    Write-Log "monitor stop | rounds=$check windows_hit=$openWindows"
}
