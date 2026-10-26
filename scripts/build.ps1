<#
  一键打包：产出可分发 zip
  用法：  .\scripts\build.ps1                              （版本号自动取 app\main.py 的 APP_VERSION）
          .\scripts\build.ps1 -Version 0.3                 （强制覆盖版本号）
          .\scripts\build.ps1 -OutRoot build\release       （产物换个地方，试打包用）
  产物：  dist\换装批量生产器\                          （文件夹版，直接能跑）
          dist\换装批量生产器-v<版本>-<日期>.zip        （发给别人用这个）

  会顺手清掉 dist 里**其他版本**的 zip（只删 zip，不动文件夹）。
#>
param(
    [string]$Version = "",
    [string]$OutRoot = "dist",
    # 代码签名（可选）：有证书时加 -Sign 即可，未签名也不影响打包
    [switch]$Sign,
    [string]$CertThumbprint = $env:CODESIGN_THUMBPRINT,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
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

# 版本信息资源：没有它，exe 属性里的公司/说明/版本全是空的，
# SmartScreen 和杀软都更容易把"无元数据的未签名程序"当可疑样本
$versionFile = Join-Path $root "build\version_info.txt"
& $python scripts\make_version_file.py --version $Version --out $versionFile
if ($LASTEXITCODE -ne 0) { throw "生成版本信息资源失败" }

Write-Host "① 关闭正在运行的旧实例（否则 exe 被占用，打包会失败）" -ForegroundColor Cyan
Get-Process | Where-Object { $_.ProcessName -like "*换装批量生产器*" } | ForEach-Object {
    Stop-Process -Id $_.Id -Force
    Write-Host "   已关闭 PID $($_.Id)"
}
Start-Sleep -Seconds 1

$appName = "换装批量生产器"
$distRoot = Join-Path $root $OutRoot
New-Item -ItemType Directory -Force $distRoot | Out-Null

Write-Host "③ 打包（onedir 模式，不要用 --onefile）" -ForegroundColor Cyan
$log = Join-Path $distRoot "build.log"
# PyInstaller 会往 stderr 打进度，不能直接走管道（会被 $ErrorActionPreference=Stop 当错误）
$ErrorActionPreference = "Continue"
& $python -m PyInstaller --noconfirm --clean --onedir --windowed --noupx `
    --name $appName --distpath $distRoot --paths "." --version-file $versionFile run_app.py *> $log
$buildCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Get-Content $log -Tail 2
if ($buildCode -ne 0) { throw "PyInstaller 打包失败，日志：$log" }

$builtDir = Join-Path $distRoot $appName
$exe = Join-Path $builtDir "$appName.exe"
if (-not (Test-Path $exe)) { throw "没有生成 $exe" }

# 随包放一个"解除锁定"入口：zip 被下载后，解压出来的 exe 会带上 MOTW（来源标记），
# 未签名程序因此会触发 SmartScreen。这个 bat 先清掉 MOTW 再启动（自己双击也可能被问一次）。
# 内容用 GBK 写，避免 cmd 把中文读成乱码。
$unblockBat = Join-Path $builtDir "解除锁定并启动.bat"
$batLines = @(
    '@echo off',
    'cd /d "%~dp0"',
    'echo 正在解除 Windows 对下载文件的锁定（每台机器只需一次）...',
    'powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath ''%~dp0'' -Recurse -File -ErrorAction SilentlyContinue | Unblock-File"',
    'echo 启动 换装批量生产器 ...',
    'start "" "换装批量生产器.exe"'
)
[IO.File]::WriteAllText($unblockBat, ($batLines -join "`r`n") + "`r`n", [Text.Encoding]::GetEncoding(936))

Write-Host "④ 自检（失败只提示，不中断）" -ForegroundColor Cyan
try {
    $probe = Start-Process -FilePath $exe -ArgumentList "--selftest" -PassThru -WindowStyle Hidden
    if (-not $probe.WaitForExit(15000)) { $probe.Kill(); Write-Host "   自检超时（不影响打包）" -ForegroundColor Yellow }
    elseif ($probe.ExitCode -ne 0) { Write-Host "   自检返回 $($probe.ExitCode)，建议手动双击试一次" -ForegroundColor Yellow }
    else { Write-Host "   自检通过" -ForegroundColor DarkGray }
} catch {
    Write-Host "   跳过自检（当前环境不允许启动 GUI 进程）：$($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "⑥ 代码签名（可选）" -ForegroundColor Cyan
if ($Sign) {
    if (-not $CertThumbprint) { throw "-Sign 需要 -CertThumbprint 或环境变量 CODESIGN_THUMBPRINT" }
    $signtool = (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source
    if (-not $signtool) {
        $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match 'x64' } | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $signtool) { throw "找不到 signtool.exe（装 Windows SDK 或改用证书厂商工具）" }
    & $signtool sign /sha1 $CertThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 $exe
    if ($LASTEXITCODE -ne 0) { throw "签名失败（检查证书是否已导入、是否过期）" }
    $sig = Get-AuthenticodeSignature $exe
    Write-Host "   签名状态：$($sig.Status)  主体：$($sig.SignerCertificate.Subject)" -ForegroundColor DarkGray
} else {
    Write-Host "   未签名 —— 客户机首次运行会被 SmartScreen 拦一次，点「更多信息 → 仍要运行」即可；" -ForegroundColor Yellow
    Write-Host "   根治要买代码签名证书，见 docs\distribution-signing.md" -ForegroundColor Yellow
}

Write-Host "⑤ 压成 zip" -ForegroundColor Cyan
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
