<#
  一键打包：产出可分发 zip
  用法：  .\scripts\build.ps1            （默认 v0.1）
          .\scripts\build.ps1 -Version 0.2
  产物：  dist\换装批量生产器\               （文件夹版，直接能跑）
          dist\换装批量生产器-v0.1-20261024.zip（发给别人用这个）
#>
param([string]$Version = "0.1")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Find-Python {
    # 有些机器上 python 是指向微软商店的假入口，必须实际跑一下才认
    foreach ($cand in @("python3", "py", "python")) {
        $exe = Get-Command $cand -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $ver = & $exe.Source -c "import sys;print(sys.version_info[0])" 2>$null
        if ($LASTEXITCODE -eq 0 -and "$ver".Trim() -eq "3") { return $exe.Source }
    }
    throw "找不到可用的 Python 3（试过 python3 / py / python）"
}

$python = Find-Python
Write-Host "使用 Python：$python" -ForegroundColor DarkGray

Write-Host "① 关闭正在运行的旧实例（否则 exe 被占用，打包会失败）" -ForegroundColor Cyan
Get-Process | Where-Object { $_.ProcessName -like "*换装批量生产器*" } | ForEach-Object {
    Stop-Process -Id $_.Id -Force
    Write-Host "   已关闭 PID $($_.Id)"
}
Start-Sleep -Seconds 1

Write-Host "② 打包（onedir 模式，不要用 --onefile）" -ForegroundColor Cyan
$log = Join-Path $root "dist\build.log"
New-Item -ItemType Directory -Force (Join-Path $root "dist") | Out-Null
# PyInstaller 会往 stderr 打进度，不能直接走管道（会被 $ErrorActionPreference=Stop 当错误）
$ErrorActionPreference = "Continue"
& $python -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name "换装批量生产器" --paths "." run_app.py *> $log
$buildCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Get-Content $log -Tail 2
if ($buildCode -ne 0) { throw "PyInstaller 打包失败，日志：$log" }

$exe = Join-Path $root "dist\换装批量生产器\换装批量生产器.exe"
if (-not (Test-Path $exe)) { throw "没有生成 $exe" }

Write-Host "③ 自检（失败只提示，不中断）" -ForegroundColor Cyan
try {
    $probe = Start-Process -FilePath $exe -ArgumentList "--selftest" -PassThru -WindowStyle Hidden
    if (-not $probe.WaitForExit(15000)) { $probe.Kill(); Write-Host "   自检超时（不影响打包）" -ForegroundColor Yellow }
    elseif ($probe.ExitCode -ne 0) { Write-Host "   自检返回 $($probe.ExitCode)，建议手动双击试一次" -ForegroundColor Yellow }
    else { Write-Host "   自检通过" -ForegroundColor DarkGray }
} catch {
    Write-Host "   跳过自检（当前环境不允许启动 GUI 进程）：$($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "④ 压成 zip" -ForegroundColor Cyan
$stamp = Get-Date -Format "yyyyMMdd"
$zip = Join-Path $root "dist\换装批量生产器-v$Version-$stamp.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $root "dist\换装批量生产器") -DestinationPath $zip -CompressionLevel Optimal

$sizeMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "完成：$zip（$sizeMb MB）" -ForegroundColor Green
Write-Host "提醒：zip 走 GitHub Releases，不要提交到代码仓库。" -ForegroundColor Yellow
