$ErrorActionPreference = 'SilentlyContinue'
$targets = @()
$c = Get-NetTCPConnection -LocalPort 5100 -State Listen
if ($c) { $targets += $c.OwningProcess }
Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -match 'app\.py' } | ForEach-Object { $targets += $_.ProcessId }
$targets = $targets | Sort-Object -Unique
if ($targets.Count -eq 0) {
    Write-Host '没有找到正在运行的电费监控进程'
} else {
    foreach ($t in $targets) {
        try {
            Stop-Process -Id $t -Force -ErrorAction Stop
            Write-Host ("已结束进程 PID " + $t)
        } catch {
            Write-Host ("进程 " + $t + " 已经退出")
        }
    }
}
