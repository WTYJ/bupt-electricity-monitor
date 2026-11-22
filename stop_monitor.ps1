$registration = Join-Path $PSScriptRoot 'electric_monitor.pid'
$instancePid = 0
try {
    $identity = @(Get-Content -LiteralPath $registration -ErrorAction Stop)
} catch {
    Write-Host '没有找到正在运行的电费监控进程'
    return
}

if ($identity.Count -notin 2, 3 -or
    ![int]::TryParse($identity[0], [ref]$instancePid) -or
    $instancePid -le 0 -or $identity[1] -notmatch '^\d+$') {
    Write-Host '没有找到有效的电费监控进程登记'
    return
}

$process = Get-Process -Id $instancePid -ErrorAction SilentlyContinue
if (!$process) {
    Write-Host '电费监控进程已退出'
    return
}

try {
    # 持有进程句柄，避免身份验证和停止之间发生 PID 复用。
    $null = $process.Handle
    if ($process.StartTime.ToFileTimeUtc().ToString() -cne $identity[1]) {
        Write-Host '登记的电费监控进程已退出'
        return
    }
    Stop-Process -InputObject $process -ErrorAction Stop
    Write-Host ("已结束电费监控进程 PID " + $instancePid)
} catch {
    Write-Warning ("无法结束登记的电费监控进程: " + $_.Exception.Message)
} finally {
    $process.Dispose()
}
