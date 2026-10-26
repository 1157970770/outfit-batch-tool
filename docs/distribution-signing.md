# 分发时的 SmartScreen 弹窗：为什么出现、怎么消掉

> 症状：双击 exe，弹出蓝色全屏窗口「**Windows 已保护你的电脑 / Microsoft Defender SmartScreen
> 阻止了无法识别的应用启动**」，只有一个「不运行」按钮（点「更多信息」才会出现「仍要运行」）。
> 这不是杀毒报毒，是**信誉拦截**。

---

## 1. 它到底在判断什么

SmartScreen 只看两件事：

| 条件 | 我们的现状 |
| --- | --- |
| 文件是否带 **MOTW**（Mark of the Web，即 `Zone.Identifier` 数据流，来自"从网上下载"） | ✅ 有（zip 一经浏览器/微信/QQ 传输，解压出来的 exe 就带） |
| 该文件 / 发布者是否**有信誉**（= 有效的**代码签名** + 足够的下载量） | ❌ 没有：`Get-AuthenticodeSignature` → **NotSigned** |

两个条件同时成立 → 每次发布的新 hash 都是"零信誉的未知程序" → 必拦。

> 顺带一提：如果弹窗里连「仍要运行」都点不出来，那是 **智能应用控制（Smart App Control）** 开着，
> 它比 SmartScreen 更硬，只能签名或关掉该功能（本机实测 `VerifiedAndReputablePolicyState = 0`，即未开启）。

---

## 2. 四种处理，按代价从低到高

### A. 让用户点一次（零成本，适合先跑起来）

点弹窗左下角「**更多信息**」→ 出现「**仍要运行**」→ 点它。每台机器对这个版本的 exe 只会问一次。

### B. 去掉 MOTW（内部小范围最划算）

没有 MOTW，SmartScreen 就不介入（它只检查"来自 Internet 的文件"）：

| 做法 | 说明 |
| --- | --- |
| 双击包里的 `解除锁定并启动.bat` | 我们随包提供的入口：先 `Unblock-File` 递归清掉 MOTW，再启动 exe。它自己可能被问一次，点「运行」即可 |
| 右键 exe → 属性 → 勾「**解除锁定**」→ 确定 | 只处理单个文件；`_internal` 里的 dll 一般不需要 |
| 命令行 | `Get-ChildItem -Recurse -File \| Unblock-File` |
| **别用浏览器/微信/QQ 传 zip** | 内网共享、移动硬盘、企业网盘直链拷贝**不产生** MOTW |
| **用 7-Zip 解压** | 资源管理器"全部解压缩"会把 MOTW 传播给每个文件；7-Zip / `Expand-Archive` 不传播 |

代价：每个收件人都要操作一次，且"来路不明"这件事本身没解决。

### C. 代码签名（唯一根治，对外分发必须）

| 证书类型 | 效果 | 参考价 |
| --- | --- | --- |
| **EV 代码签名**（硬件 token / 云签名） | **立刻**有信誉，第一次运行就不弹 | ¥2000–4000/年 |
| OV 代码签名 | 有签名但**仍需积累下载量**，前期可能还弹（可点仍要运行） | ¥800–2000/年 |

要点：

1. 2023 年起主流 CA 要求私钥存在**硬件**（USB token 或云 HSM），拿不到裸 `.pfx`；
   国内代理（如腾讯云、阿里云、沃通等）都能办，需要**营业执照**。
2. 签名必须带**时间戳**，否则证书到期后已发出的 exe 全部失效。
3. 本仓库已经接好开关：

```powershell
# 证书导入本机后，拿到指纹
Get-ChildItem Cert:\CurrentUser\My | Format-Table Subject,Thumbprint

# 打包并签名（不传 -Sign 就是普通未签名包）
.\scripts\build.ps1 -Sign -CertThumbprint <指纹>
# 或先设环境变量，之后每次打包自动签名
$env:CODESIGN_THUMBPRINT = "<指纹>"; .\scripts\build.ps1 -Sign
```

签名后自检：

```powershell
Get-AuthenticodeSignature .\dist\换装批量生产器\换装批量生产器.exe | Format-List Status,SignerCertificate
# 期望 Status = Valid
```

### D. 企业内部部署（只对"自己人"的机器有效）

- Intune / 组策略把 SmartScreen 应用检查设为"警告"或加企业白名单；
- 或改用 MSIX 安装包 + 内网证书信任链（域内分发受信任根证书），安装时不走 SmartScreen；
- 或 AppLocker / WDAC 白名单。

对外发客户**完全无效**，别指望。

---

## 3. 这些做法没用，别浪费时间

| 想法 | 为什么不行 |
| --- | --- |
| 改文件名 / 改 zip 名 / 换图标 | 判断依据是文件的哈希与签名，不是名字 |
| 用 `.bat`/`.cmd` 包一层去启动 exe | 只是把提示换了个样子，而且更容易被杀软当可疑脚本 |
| UPX 压缩减体积 | PyInstaller + UPX 是杀软误报高发组合（构建已加 `--noupx`） |
| 在程序里弹"请点仍要运行" | 弹窗发生在程序启动**之前**，代码根本没跑起来 |
| 自己生成自签名证书 | 不在 Windows 信任链里，SmartScreen 不认；只对内部 MSIX/驱动有用 |
| 删掉 `_internal` 只发 exe | 起不来（onedir 模式），且不影响拦截 |

---

## 4. 本次已做的改进（2026-10-26）

| 改动 | 作用 |
| --- | --- |
| 打包时生成**版本信息资源**（CompanyName / FileDescription / FileVersion） | exe 属性面板不再空白；少一类"无元数据可疑样本"的理由 |
| 构建加 `--noupx` | 避免 UPX 加壳带来的误报 |
| 包内附 `解除锁定并启动.bat` | 收件人双击一次即可清 MOTW 并启动 |
| `build.ps1` 增加 `-Sign` / `CODESIGN_THUMBPRINT` | 买到证书后一条命令就能签名，不用改代码 |

> 注意：**加版本信息不会让弹窗消失**——它只降低误报概率。要"不弹"，只有 B（去 MOTW）和 C（签名）。

---

## 5. 一句话结论

- **内部几个人先用**：让他们点一次「更多信息 → 仍要运行」，或跑 `解除锁定并启动.bat`；
- **要发给外部客户**：买代码签名证书（EV 立竿见影），签名后 SmartScreen 不再拦。
