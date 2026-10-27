# 模板：从本地视频到「程序能用的清单」

> 更新：**2026-10-26**。本文按**现行方案**重写：模板走**公有读直链（方案 B）**，
> 清单发布在桶根 `manifest.json`，客户端只读清单、不签名、不碰模板桶。
> 文档里写的 `D:\模板` 是占位路径，实际素材在 `D:\视频模板分类工作区`。

一句话流程：**视频上传到 `input/tpl/` 并逐对象设公有读 → 同一条命令生成清单 → 清单发到桶根 → 服务端 `manifest_url` 指向它**。

---

## 0. 为什么是"公有读直链"，不是"私有桶 + 程序签名"

模型（阿里云百炼）自己去拉模板视频，它不会带我们的签名；而客户端手里的 COS 凭证是
**只能写成品桶的子账号**，对模板桶**零权限**（`video-input-1463907213` 整个桶 `AccessDenied`）。
所以"客户端给模型签模板链接"这条路**走不通**——这一点写在
[adr-001-cos-client-credential.md](adr-001-cos-client-credential.md) §7，别重复踩。

| 方案 | 桶权限 | 清单里写什么 | 现状 |
| --- | --- | --- | --- |
| **B 公有读 + 直链** | `input/tpl/*` 与 `manifest.json` 公有读，其余私有 | `"url": "https://…/input/tpl/001.mp4"` | ✅ **现行** |
| A 私有桶 + 程序签名 | 保持私有 | `"key": "input/tpl/001.mp4"` | ❌ 不可行（签名者没读权限） |

代价：模板**任何人拿到 URL 都能下载**。素材是自有/已授权素材时才可接受。

---

## 1. 一条命令：上传 + 打标签 + 生成清单

```powershell
# 先看编号和标签，不上传
python3 tools\upload_templates.py --dry-run

# 先传 5 个试水
python3 tools\upload_templates.py --limit 5

# 全量（幂等：对象大小一致就跳过，不会重复传）
python3 tools\upload_templates.py --no-duration --out build\manifest\manifest.json
```

它做什么：

1. 按 `--root`（默认 `D:\视频模板分类工作区`）下**目录名 + 文件名顺序**稳定编号 `001…NNN`；
   目录名含「近景」→ `shot=near`，否则 `shot=medium_far`；
2. 上传成 `input/tpl/NNN.mp4`，同时打 `shot` / `kind` / `tpl_id` 三个标签；
3. **逐对象设 `public-read`**（模型要匿名拉取）；
4. 生成清单 `build/manifest/manifest.json`。

> 编号规则很重要：**「第 N 组」= 清单第 `(N-1)*100+1` ~ `N*100` 项**，按清单顺序切，不按文件名。

---

## 2. 清单长什么样

```json
{
  "version": 1,
  "count": 108,
  "prefix": "input/tpl",
  "templates": [
    { "id": "001", "name": "10月8日(1)", "shot": "medium_far", "size_mb": 5.57,
      "duration": null,
      "url": "https://video-input-1463907213.cos.ap-guangzhou.myqcloud.com/input/tpl/001.mp4" }
  ]
}
```

客户端只认 `id` / `url`（`key` 模式时才用 `key`）；`shot`、`size_mb`、`duration` 是给人看的，不影响出片。

---

## 3. 发布清单（覆盖写，URL 永不变）

```powershell
python3 tools\publish_manifest.py              # 上传 build\manifest\manifest.json 到桶根 + 设公有读
python3 tools\publish_manifest.py --verify-only  # 只验证：匿名拉取 + 客户端视角解析
```

发布地址（固定）：

```
https://video-input-1463907213.cos.ap-guangzhou.myqcloud.com/manifest.json
```

> 客户端的 `manifest_url` 就是它，**每次启动重拉一次、不缓存**。换清单只覆盖写同名对象，客户端不需要动。
> 旧的 `cdn.ap-shanghai.tencentcos.cn` 域名已废弃（连接层 502），别再改回去。

---

## 4. 服务端配置（客户端看不到这个文件）

```json
{
  "wan": {
    "provider": "dashscope",
    "api_key": "sk-百炼 Key",
    "model": "wan3.0-video",
    "base_url": "https://dashscope.aliyuncs.com/api/v1"
  },
  "manifest_url": "https://video-input-1463907213.cos.ap-guangzhou.myqcloud.com/manifest.json",
  "cos": {
    "enabled": true,
    "bucket": "video-input-1463907213",
    "template_bucket": "video-input-1463907213",
    "output_bucket": "video-output-1463907213",
    "region": "ap-guangzhou",
    "prefix": "prod",
    "source_prefix": "",
    "domain": "",
    "secret_id": "子账号 SecretId",
    "secret_key": "子账号 SecretKey"
  }
}
```

| 字段 | 作用 |
| --- | --- |
| `template_bucket` | 模板桶（只读语义）；客户端对它**没有任何权限**，靠公有直链 |
| `output_bucket` | 成品桶，客户端用下发的子账号密钥写入 |
| `prefix` | 统一前缀，**当前 `prod`**；成品落 `prod/output/…`（CAM 策略按这个写） |
| `secret_id` / `secret_key` | 子账号 `rh-client-uploader`，只能对 `video-output/…/prod/output/*` Put/Get/分片 |

改完 `server\config.json` → `python3 scripts\license_deploy.py deploy` → 客户端**下次启动**生效。

---

## 5. 产物落在哪

```
video-output-1463907213/
└── prod/                        ← cos.prefix（当前 prod）
    └── output/
        └── 2026-10-26/
            └── 2026-10-26a/     ← 批次号（统一小写）
                ├── 001-9c1b2d4e.mp4   ← 编号-内容指纹，重复跑不会多存
                └── 002-44e8557f.mp4
```

> 归档失败**不影响出片**：异常只写进 `archive_error`。所以"任务成功但桶里没有"时，
> 先查 CAM 策略是否写了 `prod/output/*`（只写 `output/*` 会 403 静默失败）。

---

## 6. 验收（四步，别跳）

1. 浏览器打开清单地址 → 能下载 JSON，条数与本地一致；
2. 浏览器打开任意一条 `input/tpl/NNN.mp4` → **能在浏览器里直接播放**（不是 403）；
3. 客户端启动 → 状态灯「模板清单：108 个」；
4. 跑「随机 3 条」→ 真的出片（要花钱）。

---

## 7. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 清单 403 | 对象没设公有读 / 发布失败 | 重跑 `publish_manifest.py`；确认 `put_object_acl` 没报错 |
| 模板 403（模型拉不到） | 单条对象漏设 `public-read` | 重跑 `upload_templates.py --force`，或单独补 ACL |
| 客户端显示「模板清单：加载失败」 | `manifest_url` 打不开 / JSON 格式不对 | `publish_manifest.py --verify-only` |
| **第 2–5 组跑不了**（"模板不足 100 个"） | 清单只有 108 条，分组按 100 切 | 补到 500，或先用「随机 100 条」 |
| 任务成功但桶里没成品 | CAM 少 `prod/output/*`，或子账号密钥过期 | 看 `archive_error`；核对策略前缀 |
| 改了清单但客户端没变 | 客户端只认服务端下发的 `manifest_url` | 覆盖写同名对象即可；地址没变就不动配置 |

---

## 8. 补到 500 个的差距

| 项 | 现状 | 目标 |
| --- | --- | --- |
| 模板总数 | 108（近景 14 / 中远 94） | 500 |
| 可用分组 | 第 1 组 + 随机 100 | 第 1–5 组 |
| `TOTAL_TEMPLATES` | 代码里写死 500（`app/core.py:43`） | 补齐素材即可，不用改代码 |

> 素材补齐后：`upload_templates.py` 全量跑一遍 → `publish_manifest.py` 发布 → 客户端重启就看到新数量。
> 数量长期不足 500 时，建议把分组改成按实际条数动态算，别让用户点到跑不了的分组。
