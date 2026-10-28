# 官方版 · 单文件 exe 构建（PyInstaller --onefile）
# 产物：dist-official-onefile\换装批量生产器-官方版.exe
# 运行时会解压到 %TEMP%\_MEIxxxx，用户看不到；正常退出自动清理。
param(
    [string]$OutRoot = "dist-official-onefile"
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Find-Python {
    foreach ($cand in @("python3", "py", "python")) {
        $exe = Get-Command $cand -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $ver = & $exe.Source -c "import sys;print(sys.version_info[0])" 2>$null
        if ($LASTEXITCODE -eq 0 -and "$ver".Trim() -eq "3") { return $exe.Source }
    }
    throw "找不到可用的 Python 3"
}

$python = Find-Python
$appName = "换装批量生产器-官方版"
$distRoot = Join-Path $root $OutRoot
$workRoot = Join-Path $root "build\onefile-work"
$specRoot = Join-Path $root "build\onefile-spec"
$versionFile = Join-Path $root "build\version_info.txt"

New-Item -ItemType Directory -Force $distRoot | Out-Null
New-Item -ItemType Directory -Force $workRoot | Out-Null
New-Item -ItemType Directory -Force $specRoot | Out-Null

$version = (Select-String -Path (Join-Path $root "app\main.py") -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' |
    Select-Object -First 1).Matches[0].Groups[1].Value
if (-not $version) { throw "没能从 app\main.py 读到 APP_VERSION" }
& $python scripts\make_version_file.py --version $version --out $versionFile
if ($LASTEXITCODE -ne 0) { throw "生成版本信息资源失败" }

Write-Host "版本：v$version  单文件输出：$distRoot" -ForegroundColor DarkGray

Get-Process | Where-Object { $_.ProcessName -like "*换装批量生产器-官方版*" } |
    ForEach-Object { Stop-Process -Id $_.Id -Force; Write-Host "已关闭 PID $($_.Id)" }
Start-Sleep -Seconds 1

$log = Join-Path $workRoot "build-onefile.log"
$ErrorActionPreference = "Continue"
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
    --name $appName --distpath $distRoot --workpath $workRoot --specpath $specRoot `
    --paths $root --add-data "$root\prompts\prompt-library.json;prompts" `
    --hidden-import app.cos_rest --exclude-module numpy `
    --runtime-tmpdir "%LOCALAPPDATA%\RHOutfitToolWan3\run" `
    --version-file $versionFile run_app.py *> $log
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Get-Content $log -Tail 3
if ($code -ne 0) { throw "PyInstaller onefile 打包失败，日志：$log" }

$exe = Join-Path $distRoot "$appName.exe"
if (-not (Test-Path $exe)) { throw "没有生成 $exe" }

$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash
Write-Host ""
Write-Host "完成：$exe（$sizeMb MB）" -ForegroundColor Green
Write-Host "SHA256：$hash" -ForegroundColor DarkGray
Write-Host "说明：单 exe；运行时解压到 %LOCALAPPDATA%\RHOutfitToolWan3\run\_MEIxxxx，正常退出自动清理。" -ForegroundColor DarkGray
Write-Host "提醒：未签名单文件更容易被 SmartScreen/杀软拦截，正式分发建议代码签名。" -ForegroundColor Yellow
