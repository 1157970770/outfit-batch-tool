<#
  一键打包：产出可分发 zip
  用法：  .\scripts\build.ps1                              （版本号自动取 app\main.py 的 APP_VERSION）
          .\scripts\build.ps1 -Version 0.3                 （强制覆盖版本号）
          .\scripts\build.ps1 -LicenseServer https://xxx   （包内 配置.ini 预填授权地址）
          .\scripts\build.ps1 -OutRoot build\release       （产物换个地方，试打包用）
  产物：  dist\换装批量生产器\                          （文件夹版，直接能跑）
          dist\换装批量生产器-v<版本>-<日期>.zip        （发给别人用这个）

  会顺手清掉 dist 里**其他版本**的 zip（只删 zip，不动文件夹）。
#>
param(
    [string]$Version = "",
    [string]$LicenseServer = "",
    [string]$OutRoot = "dist"
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
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

# 版本号只有一个真源：app\main.py 的 APP_VERSION，避免包名和界面标题对不上
if (-not $Version) {
    $m = Select-String -Path (Join-Path $root "app\main.py") -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' |
         Select-Object -First 1
    if ($m) { $Version = $m.Matches[0].Groups[1].Value } else { throw "没能从 app\main.py 读到 APP_VERSION" }
}
Write-Host "版本：v$Version    产物目录：$OutRoot" -ForegroundColor DarkGray

$python = Find-Python
Write-Host "使用 Python：$python" -ForegroundColor DarkGray

Write-Host "① 关闭正在运行的旧实例（否则 exe 被占用，打包会失败）" -ForegroundColor Cyan
Get-Process | Where-Object { $_.ProcessName -like "*换装批量生产器*" } | ForEach-Object {
    Stop-Process -Id $_.Id -Force
    Write-Host "   已关闭 PID $($_.Id)"
}
Start-Sleep -Seconds 1

$appName = "换装批量生产器"
$distRoot = Join-Path $root $OutRoot
New-Item -ItemType Directory -Force $distRoot | Out-Null

Write-Host "② 备份旧 配置.ini（PyInstaller 会整目录重建，不备份就丢了）" -ForegroundColor Cyan
$cfgSrc = Join-Path $distRoot "$appName\配置.ini"
if (Test-Path $cfgSrc) {
    $cfgBak = Join-Path $distRoot ("配置.ini.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    Copy-Item $cfgSrc $cfgBak -Force
    Write-Host "   已备份 → $cfgBak" -ForegroundColor DarkGray
} else {
    Write-Host "   （没有旧配置，跳过）" -ForegroundColor DarkGray
}

Write-Host "③ 打包（onedir 模式，不要用 --onefile）" -ForegroundColor Cyan
$log = Join-Path $distRoot "build.log"
# PyInstaller 会往 stderr 打进度，不能直接走管道（会被 $ErrorActionPreference=Stop 当错误）
$ErrorActionPreference = "Continue"
& $python -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name $appName --distpath $distRoot --paths "." run_app.py *> $log
$buildCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Get-Content $log -Tail 2
if ($buildCode -ne 0) { throw "PyInstaller 打包失败，日志：$log" }

$builtDir = Join-Path $distRoot $appName
$exe = Join-Path $builtDir "$appName.exe"
if (-not (Test-Path $exe)) { throw "没有生成 $exe" }

Write-Host "④ 自检（失败只提示，不中断）" -ForegroundColor Cyan
try {
    $probe = Start-Process -FilePath $exe -ArgumentList "--selftest" -PassThru -WindowStyle Hidden
    if (-not $probe.WaitForExit(15000)) { $probe.Kill(); Write-Host "   自检超时（不影响打包）" -ForegroundColor Yellow }
    elseif ($probe.ExitCode -ne 0) { Write-Host "   自检返回 $($probe.ExitCode)，建议手动双击试一次" -ForegroundColor Yellow }
    else { Write-Host "   自检通过" -ForegroundColor DarkGray }
} catch {
    Write-Host "   跳过自检（当前环境不允许启动 GUI 进程）：$($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "⑤ 写干净的 配置.ini（不含任何 Key）" -ForegroundColor Cyan
# 自检会把 配置.ini 生成出来，可能带着开发机填过的 Key，必须用模板覆盖
$mkArgs = @((Join-Path $root "scripts\make_config.py"), "--dir", $builtDir)
if ($LicenseServer) { $mkArgs += @("--license-server", $LicenseServer) }
& $python @mkArgs
if ($LASTEXITCODE -ne 0) { throw "生成 配置.ini 失败" }

Write-Host "⑥ 压成 zip" -ForegroundColor Cyan
$stamp = Get-Date -Format "yyyyMMdd"
$zip = Join-Path $distRoot "$appName-v$Version-$stamp.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $builtDir -DestinationPath $zip -CompressionLevel Optimal

Write-Host "⑦ 清掉其他版本的 zip（保留刚生成的这个）" -ForegroundColor Cyan
Get-ChildItem -Path $distRoot -Filter "$appName-v*.zip" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -ne $zip } |
    ForEach-Object {
        Remove-Item $_.FullName -Force
        Write-Host "   已删除旧包：$($_.Name)" -ForegroundColor DarkGray
    }

$sizeMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "完成：$zip（$sizeMb MB）" -ForegroundColor Green
Write-Host "文件夹版：$builtDir" -ForegroundColor Green
Write-Host "提醒：zip 走 GitHub Releases，不要提交到代码仓库。" -ForegroundColor Yellow
