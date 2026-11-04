#Requires -Version 5.1
<#
    Register a daily Windows Scheduled Task that backs up the tag store
    (web-data/template_tags.json) to Tencent COS.

    Usage (no admin required, current user only):
        powershell -NoProfile -ExecutionPolicy Bypass -File install_tag_backup_task.ps1
        powershell ... -File install_tag_backup_task.ps1 -Time 10:30 -TaskName MyTask

    Uninstall:
        powershell ... -File install_tag_backup_task.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$Time = "10:30",
    [string]$TaskName = "TagStoreBackupDaily",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$scriptsDir = $PSScriptRoot
$root = Split-Path -Parent $scriptsDir
$target = Join-Path $scriptsDir "backup_tag_store.py"

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
    -Description "Back up web-data/template_tags.json to Tencent COS (created $stamp)" `
    -Force | Out-Null

$info = Get-ScheduledTask -TaskName $TaskName
$next = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
Write-Host "[OK] task registered: $($info.TaskName)  state=$($info.State)  next=$next"
Write-Host "Run it now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check log :  $root\web-data\logs\tag_backup.log"
