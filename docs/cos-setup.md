# 腾讯云 COS 接入手册（本项目专用）

> 目标：让 100 条成片不再只躺在本机磁盘、也不再依赖 RunningHub 那个 **24 小时就失效**的链接。
> 读完这一篇，你应该能在 30 分钟内把桶建好、密钥配好、自检跑通。

---

## 0. 先搞清腾讯云的叫法

| 通用概念 | 腾讯云叫法 | 本项目里的位置 |
| --- | --- | --- |
| 对象存储 | **COS**（Cloud Object Storage） | `app/storage.py` 的 `CosStorage` |
| 存储桶 | **存储桶 Bucket**，名字**必须带 APPID**，如 `video-prod-1250000000` | `COS_BUCKET` |
| 地域 | **地域 Region**，如 `ap-guangzhou` | `COS_REGION` |
| 访问密钥 | **SecretId / SecretKey**（建议用子账号的） | `COS_SECRET_ID` / `COS_SECRET_KEY` |
| 对象名 | **对象键 Key**，如 `output/2026-10-24/20261024A/001-9c1b2d4e.mp4` | `object_key()` 生成 |
| 临时凭证 | **临时密钥 STS** | 本期未用（预签名够用） |
| 权限 | **CAM 策略** | 见第 3 节 |
| 访问域名 | `https://<bucket>.cos.<region>.myqcloud.com` | `public_url()` |

**最容易错的一条**：桶名不是 `video-prod`，而是 `video-prod-1250000000`。少了 `-APPID` 会直接 `NoSuchBucket`。

---

## 1. 建桶（控制台 3 分钟）

1. 打开 <https://console.cloud.tencent.com/cos>，首次使用需实名认证。
2. **存储桶列表 → 创建存储桶**：
   - 名称：`video-prod`（系统自动补 `-你的APPID`）
   - 地域：选**离你和用户最近**的。国内常用 `ap-guangzhou`（广州）、`ap-shanghai`（上海）、`ap-beijing`（北京）。**建完不能改地域**，只能重建。
   - 访问权限：**私有读写**（不要选公有读）
   - 版本控制：建议**开启**（误删可恢复；代价是历史版本也计费，可配 30 天清理）
   - 服务端加密：默认 SSE-COS 即可
   - 日志存储：量大再开，调试期可关
3. 建完进桶 → **概览**，记下「所属地域」和「访问域名」，后面要用。

---

## 2. 拿密钥（不要用主账号）

主账号密钥 = 整个账号的钥匙，泄露等于公司云资源全丢。正确做法是建子账号：

1. **访问管理 CAM → 用户 → 用户列表 → 新建用户**
2. 类型选「自定义创建」，访问方式勾 **编程访问**（会生成 SecretId / SecretKey）
3. 下一步**不直接给预设策略**（预设的 `QcloudCOSFullAccess` 权限太大），先用「自定义策略」，内容见第 3 节
4. 创建完成后**立刻复制 SecretKey**——它只显示这一次，关掉页面就再也看不到，只能重置。

---

## 3. 最小权限策略（直接抄）

CAM → 策略 → 新建自定义策略 → 按策略语法创建：

```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": [
        "cos:PutObject",
        "cos:GetObject",
        "cos:HeadObject",
        "cos:DeleteObject",
        "cos:GetBucket",
        "cos:GetService"
      ],
      "resource": [
        "qcs::cos:ap-guangzhou:uid/1250000000:video-prod-1250000000/*"
      ]
    }
  ]
}
```

要点：

- `ap-guangzhou` 换成你的地域；`1250000000` 换成你的 **APPID**；桶名换成你的。
- 结尾 `/*` 表示桶内所有对象。想更严格就写死前缀，如 `.../video-prod-1250000000/output/*`——但这样自检脚本的 `_healthcheck/` 会被拒，需要额外加一条。
- 不要给 `cos:*`，更不要给 `*`。这份策略只能读写你指定的桶，删不掉别的资源。
- 策略生效有几十秒延迟，刚建完报 `AccessDenied` 先等 1 分钟。

---

## 4. 配置到本机（两种方式，任选）

### 方式 A：环境变量（推荐，脚本/CLI 用）

```powershell
# PowerShell（当前窗口有效）
$env:COS_BUCKET    = "video-prod-1250000000"
$env:COS_REGION    = "ap-guangzhou"
$env:COS_SECRET_ID = "AKIDxxxxxxxxxxxxxxxxxxxx"
$env:COS_SECRET_KEY= "xxxxxxxxxxxxxxxxxxxxxxxxxx"
$env:COS_PREFIX    = "prod"          # 可选，隔离环境
```

```bat
:: cmd（当前窗口有效）
set COS_BUCKET=video-prod-1250000000
set COS_REGION=ap-guangzhou
set COS_SECRET_ID=AKIDxxxxxxxxxxxxxxxxxxxx
set COS_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxx
```

需要长期生效：`setx COS_BUCKET "..."`（新开窗口生效）。**不要写进代码、不要提交 Git。**

### 方式 B：界面设置（桌面版用）

`设置 → 启用 COS 归档`，填桶名/地域/SecretId/SecretKey，点 **测试连接**：

- `✓ 读写正常` → 配置正确，可以开工
- `✗ ...` → 按第 8 节对照表排查

密钥会明文存到 `%APPDATA%\RHOutfitTool\config.json`。这是本机程序，可接受；但如果这台机器多人共用，请改用方式 A。

### 方式 C：什么都不填，复用本机已有凭证（最省事）

如果这台机器上已经配过 COS（比如 WorkBuddy 那套 `tccli auth login`，或放了 `~/.cos/credentials.json`），
界面里**只需勾上「启用 COS 归档」+ 填桶名**，密钥留空即可。

程序按这个优先级自动找凭证：

```
① 环境变量  COS_SECRET_ID / COS_SECRET_KEY（+ COS_SECURITY_TOKEN）
② ~/.cos/credentials.json      {secret_id, secret_key, token, region, bucket}
③ ~/.tccli/<profile>.credential   tccli OAuth 临时凭证（约 2 小时，带 token）
```

- profile 默认读 `COS_TCCLI_PROFILE`，没设就用 `agent1`；过期会直接提示 `tccli auth login --profile agent1`。
- 第 ③ 种是 STS 临时凭证，程序会在 `CosConfig` 里带 `Token`，能正常签名。
- 优先级设计成「长期密钥 > 临时密钥」，是因为 OAuth 过期后文件仍残留，放最后才不会拿到失效凭证。

验证：设置里点 **测试连接**，`✓ 读写正常` 就成了，全程不用手抄 SecretKey。

---

## 5. 自检（先跑这个，再谈批量）

```bash
pip install cos-python-sdk-v5
python3 scripts/cos_check.py
```

它按顺序做：**写入 → 生成签名链接 → 读元信息 → 判断存在 → 列举 → 删除**，每步打 ✓/✗。

预期输出：

```
配置：
  存储桶   video-prod-1250000000
  地域     ap-guangzhou
  SecretId AKID***xxxx
  前缀     prod
✓ 写入对象（put）（312 ms）...
✓ 生成签名链接（sign）（0 ms）：https://video-prod-1250000000.cos.ap-guangzhou.myqcloud.com/...
...
全部通过。
```

把打印出的签名链接粘到浏览器，能下载到 `cos-check ...` 这行字，说明链路彻底通了。

---

## 6. 生命周期规则（省钱 + 自动清理）

控制台 → 你的桶 → **基础配置 → 生命周期**，加三条：

| 规则 | 前缀 | 动作 | 为什么 |
| --- | --- | --- | --- |
| 成品降冷 | `output/` | 30 天后转**低频**，90 天后转**归档**，365 天删除 | 冷数据单价只有标准存储的 1/3 ~ 1/4 |
| 素材清理 | `input/` | **7 天**后删除 | 定妆图/服装图传完就没用了 |
| 碎片清理 | 全桶 | **3 天**后删除未完成的分片上传 | 中断的分片会一直计费，且看不见 |

⚠️ 归档类型（ARCHIVE）取回要**先解冻**，几分钟到几小时，且按次收费。**对外要随时能下载的成品不要直接进归档**——要么放标准/低频，要么接受解冻等待。

---

## 7. 需要"发给客户看"时

优先级从低到高：

1. **签名 URL（默认）**：`storage.sign_url(key, 3600)`，1 小时有效，私有桶也能用。适合内部预览、临时发人。
2. **CDN 加速**：量大再上。CDN 控制台 → 域名管理 → 添加域名 → 源站类型选 **COS 源站** → 选桶。
   - 关键一步：**开启回源鉴权**（否则 CDN 回源会 403，因为桶是私有的）
   - 再配 **鉴权配置**（鉴权 A/B/C 型），否则谁拿到 CDN 链接都能白嫖流量
3. **公有读桶**：只在"完全公开的素材"场景用，且必须配 **防盗链 Referer 白名单**，否则会被别的站盗刷流量。

> 本项目默认走方案 1。`COS_DOMAIN` 只影响 `public_url()`；签名仍走 COS 源站域名。

---

## 8. 报错对照表

| 现象 | 错误码 | 真正原因 | 怎么修 |
| --- | --- | --- | --- |
| 403 | `SignatureDoesNotMatch` | SecretKey 错了/多了空格，或本机时间偏差 | 重新复制 Key；`w32tm /resync` 校时 |
| 403 | `InvalidAccessKeyId` | SecretId 不存在或被禁用 | 确认用子账号的 Id，没被停用 |
| 403 | `AccessDenied` | 策略没覆盖这个桶/前缀 | 对照第 3 节改 CAM 策略，等 1 分钟 |
| 404 | `NoSuchBucket` | 桶名少了 `-APPID`，或地域写错 | 桶名必须 `name-1250000000`；地域 `ap-xxx` |
| 403 | `RequestTimeTooSkewed` | 本机时间和标准时间差 > 15 分钟 | 校准系统时间 |
| 403 | `Request has expired` | 预签名链接过期 | 重新签；有效期别超过 7 天 |
| 400 | `KeyTooLong` / `InvalidArgument` | Key 有非法字符或超 1024 字节 | 用 `object_key()` 生成，别手拼中文 |
| 411 | `MissingContentLength` | 上传没带长度 | 用 SDK 的 `upload_file`/`put_object`，别自己拼 HTTP |

本项目已经把常见错误翻译成人话（`describe_error()`），会带上上面的排查提示。

---

## 9. 费用到底多少（2026 年量级，以官网为准）

按你的场景算：7 秒 720P 竖屏 ≈ 20 MB/条，100 条 ≈ **2 GB**。

| 项目 | 单价（参考） | 你的用量 | 月成本 |
| --- | --- | --- | --- |
| 标准存储 | ≈ 0.118 元/GB/月 | 2 GB | ≈ 0.24 元 |
| 外网下行流量 | ≈ 0.50 元/GB | 假设被下载 10 次 = 20 GB | ≈ 10 元 |
| 请求 | ≈ 0.01 元/万次 | 几千次 | 忽略 |

结论：**存储几乎免费，贵的是外网流量**。所以：

- 同地域的云服务器 ↔ COS 走**内网**，流量免费（脚本批量转存时把机器放在同地域）；
- 对外分发上 CDN（单价通常比 COS 外网低）；
- 别拿公网 URL 反复拉大文件做测试。

---

## 10. 安全清单（交付前逐条打勾）

- [ ] 用的是**子账号**密钥，不是主账号
- [ ] CAM 策略限定到具体桶，没有 `cos:*`
- [ ] 桶是**私有读写**，没有图省事改成公有读
- [ ] SecretKey 只在环境变量 / 本机 config.json，`git grep AKID` 搜不到
- [ ] 签名有效期 ≤ 7 天，对外链接尽量 1 小时
- [ ] 生命周期里配了「未完成分片 3 天清理」
- [ ] 开了版本控制；或明确接受"删了就是删了"
- [ ] 账单设了**告警阈值**（防密钥泄露被刷）

---

## 11. 本项目怎么用（三条命令）

```bash
# 1) 连通性自检（不花钱，几十字节）
python3 scripts/cos_check.py

# 2) 把历史成片补传（可重复执行，同一内容不会重复存）
python3 scripts/archive_to_cos.py --root out --dry-run     # 先看计划
python3 scripts/archive_to_cos.py --root out               # 真传

# 3) 新批次：桌面版「设置」里勾上「启用 COS 归档」即可
#    CLI 用户可以直接跑已有的 run 命令，产物仍在本地；再执行第 2 步补传
```

产物落点：

```
video-prod-1250000000/
└── output/
    └── 2026-10-24/
        └── 20261024A/
            ├── 001-9c1b2d4e.mp4     ← 文件名-内容指纹，天然幂等
            └── 002-44e8557f.mp4
```

索引文件：`out/cos-index.csv`（批次/文件名/Key/大小/签名链接），可直接给运营下载用。

---

## 12. 下一步可以做什么

- **STS 临时密钥**：如果要做网页/小程序直传，用后端发 15 分钟临时凭证，前端直接 PUT 到 COS，不经过服务器。
- **CDN + 鉴权 A**：对外分发量大时再上，注意私有桶必须开回源鉴权。
- **数据万象（CI）**：视频截帧、转码、加封面，直接在 COS 上做，不用拉回本机。
- **跨地域复制**：灾备或异地分发。
