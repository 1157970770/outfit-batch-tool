#Requires -Version 5.1
<#
    Register a daily Windows Scheduled Task that syncs per-account daily
    production stats (jobs -> per-day per-owner success count / spend)
    into the Feishu bitable "每日消费表".

    Run this from the RUNTIME area (D:\批量视频任务运行区) so the task
    aggregates the production web-data; it must run AFTER deploy_runtime.py.

    Usage (no admin required, current user only):
        powershell -NoProfile -ExecutionPolicy Bypass -File install_feishu_report_task.ps1
        powershell ... -File install_feishu_report_task.ps1 -Time 18:00 -TaskName FeishuDailyReport

    Uninstall:
        powershell ... -File install_feishu_report_task.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$Time = "18:00",
    [string]$TaskName = "FeishuDailyReport",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$scriptsDir = $PSScriptRoot
$root = Split-Path -Parent $scriptsDir
$target = Join-Path $scriptsDir "feishu_daily_report.py"
$config = Join-Path $root "server\feishu.json"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[OK] removed task: $TaskName"
    } else {
        Write-Host "[SKIP] task not found: $TaskName"
    }
    return
}

if (-not (Test-Path $target)) { throw "script not found: $target" }
if (-not (Test-Path $config)) { throw "config not found: $config (app_id/app_secret/app_token/table_id)" }

# Locate a windowless interpreter (pythonw.exe) -- avoids a console flash every day.
$candidates = @(
    (Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"),
    (Join-Path $env:USERPROFILE ".local\bin\pythonw.exe")
)
$pythonw = $null
foreach ($c in $candidates) { if (Test-Path $c) { $pythonw = $c; break } }
if (-not $pythonw) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pythonw = $cmd.Source }
}
if (-not $pythonw) { throw "pythonw.exe not found; install Python or pass a working runtime first" }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host "root    : $root"
Write-Host "pythonw : $pythonw"
Write-Host "script  : $target"
Write-Host "time    : daily $Time"

# 默认窗口 D-3..今天：当天写快照（18:00 的当天进度），次日自动修正；
# 幂等 upsert，重复跑不会产生重复行。
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$target`" --quiet" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Sync per-account daily production stats to Feishu bitable (created $stamp)" `
    -Force | Out-Null

$info = Get-ScheduledTask -TaskName $TaskName
$next = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
Write-Host "[OK] task registered: $($info.TaskName)  state=$($info.State)  next=$next"
Write-Host "Run it now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check log :  $root\web-data\logs\feishu_report.log"
