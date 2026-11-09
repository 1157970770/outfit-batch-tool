# PS 助手 · 去紫（v0.18.0）

> 目标：AI 多轮生产会把同一件白料越做越紫/发品红（每轮再喂回去，偏色累积）。
> 这里把「去紫」单独做成网页 tab：选一张图 → 调强度 → 并排看前后 → 下载无损 PNG。
> 首版只做去紫，不碰视频、不接 PS 本体；判图/修图那一套三段式流水线仍在外部的
> `D:\定妆自动化区\plugins\dsh-ps-assistant`，本仓库不依赖它。

## 1. 界面在哪

| | 说明 |
| --- | --- |
| tab | `PS助手`（`index.html`，`data-view="ps-view"`，排在「素材检索」后；所有登录用户可见） |
| 前端 | `app/web/static/ps.js`（`window.PsUI = { enter, reset }`）+ `ps.css` |
| 入口 | 首次点 tab 时由 `app.js::switchView` 调 `PsUI.enter()` 绑定事件，`restoreView` 白名单含 `ps-view` |
| 上传 | 复用 `image-slots.js` 的 `uploadImages`（单槽位）：`POST /api/uploads`，格式/20MB 校验、进度条都在那边 |
| 对比 | 左「原图」（`/api/uploads/<id>`）、右「去紫后」（`/api/ps/result/...`），两侧都带尺寸/缓存标记 |

强度滑杆改动后，右侧结果会置灰并提示「强度已改，点『去紫』重算」，同时禁用下载——
避免拿着旧参数的图去发布。

## 2. 算法口径（`app/depurple.py`）

来源：外部插件的 `bridge.py:249-361`（`_rgb2lab` / `_lab2rgb` / `op_depurple`），
按「两区自包含」铁律**抄成本仓库模块**，只依赖 `numpy` + `Pillow`，不 import 任何 `app.web.*`。

1. sRGB → Lab（D65，矩阵 `[[0.4124,0.3576,0.1805],[0.2126,0.7152,0.0722],[0.0193,0.1192,0.9505]]`，
   白点 `0.95047/1/1.08883`）；**只改 A/B，L 一点不动**，所以亮度、对比、细节都不变。
2. 两个锚点（都在低饱和像素里取中位数，饱和度 `sat = max-min < 30`）：
   - 暗部：`Lg < 60`；
   - 背景：`Lg` 32 档直方图里 `>=120` 的最高峰那一档；
   - 某带不足 300 px 视为没有该锚点（互相兜底，都空则整幅不动）。
3. 扣多少按亮度插值：`off(Lg) = dark + (bg - dark) * clip((Lg-50)/max(1, hi_b-50), 0, 1)`
   ——暗部按暗部锚点、亮部按背景锚点，中间线性过渡。
4. 高光 taper：`Lg > 215` 起最多少扣 25%（白料高光自带偏色更小，全量扣会发青）。
5. 暖色保护：`A>2 且 B>2`（肤色、棕色、米色）只按 10% 扣，避免把人脸改成冷白。
6. 自校准：应用后对 `out[::3, ::3]` 抽样重测残留，`|A|` 或 `|B| > 1.0` 就把两个锚点各推一次残差、重算一遍。
7. 输出 PNG 无损，原子写（`<name>.tmp` → `os.replace`）；先 `ImageOps.exif_transpose`
   把手机竖拍转正，否则前后对比会一张躺一张立。

`strength` 是 0–100 的整数（默认 100）：`w_app = (1 - 0.9*warm) * strength/100`，
所以 0 是恒等变换（差 1 个灰阶以内，纯量化误差），50 大约是扣一半。

## 3. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/ps/depurple` | body `{upload_id, strength?}` → `{ok, upload_id, strength, width, height, name, url, download_url, cast_before, cast_after, anchors, elapsed_ms, cached}` |
| GET | `/api/ps/result/<upload_id>?strength=N[&download=1]` | 取结果 PNG；`download=1` 走附件（文件名 `<原名>_去紫_s<N>.png`），否则 inline 给 `<img>` |

- **权限**：本人严格匹配（走 `WebApplication.resolve_upload`）；别人的图一律 404（不泄露 id 是否有效）；
  管理员放行（与 `/api/uploads/<id>` 一致）。
- **错误码**：`400` 参数无效 / `404` 找不到或原图已失效 /
  `422` 解码失败 / `503` 缺 numpy / `500` 处理失败（含内存不足）。**不设像素上限**。
- **缓存**：结果路径完全由 `upload_id + strength` 派生
  （`<data_dir>/ps/<upload_id>_s<strength>.png`），旁边 `<同名>.json` 是统计侧车；
  侧车存在且不旧于原图 = 命中，重复点同一强度秒回（返回里 `cached: true`）。
  换强度就是另一个文件，互不覆盖。

## 4. 已知限制

- **不设像素上限，改由内存兜底**：整图一次性 numpy 运算（不分块），峰值内存约「像素数 × 100 字节」
  —— 4MP ≈ 400MB、24MP ≈ 2.4GB、48MP ≈ 5GB。算不下时 numpy 抛 `MemoryError`，接口回
  `500`「内存不足：这张图太大……请先把长边缩到 3000 像素左右再试」。
  要支持任意大图（且不吃满内存）需改成分块 + 代理锚点，等真有需求再做。
  **注意**：这张图会占着服务进程的内存到处理结束，别和跑批高峰撞在一起。
- **串行**：`server.py` 里模块级 `_PS_LOCK` 包住处理，避免两个人同时点把内存翻倍；
  代价是排队（实测 2048² 约 2 秒、1677×2500 约 3.3 秒）。
- **只处理图片**：视频的色偏不在本 tab 范围（视频要重生成，不去修）。
- **结果不自动清理**：`web-data/ps/` 只增不减，和 `uploads/` 一样没有 GC；
  可以随时整目录删掉（删了只是下次重算），别删 `uploads/` 里被 job/batch 引用的原图。
- **缺 numpy 不影响生产**：模块顶层用 try/except 导入，缺了只让本 tab 的接口返回 503，
  任务/批次照常跑。两个启动器用的 DSH 解释器自带 numpy；自建 `.venv` 跑测试时要 `pip install numpy`
  （已写进 `requirements.txt`）。

## 5. 测试

```bash
python tests/test_depurple.py     # 算法性质：扣偏色 ≥80%、L 漂移 ≤2、强度 0 恒等、暖色保护、锚点退化、落盘/缓存/EXIF
python tests/test_web_ps.py       # HTTP 端到端：上传→处理→取图→缓存→owner 隔离/admin 放行→400/404/401
```

两套都在 `tests/run_all.py` 的 `REQUIRED` 里；缺 numpy 的解释器会打印 `[SKIP]` 并以 0 退出
（不会把「没装依赖」误报成失败）。

## 6. 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| 页面报「PS 助手暂不可用：缺少 numpy」 | 该解释器没装 numpy；装好重启服务（`requirements.txt` 已加） |
| 500「内存不足：这张图太大」 | 大图吃满内存；把长边缩到 3000 像素左右再试，或换到内存更大的机器 |
| 结果和原图看着没差别 | 图里没有低饱和像素/没有暗部，锚点退化（返回 `anchors` 全是 0）；这类图本来就不该做去紫 |
| 结果偏青 | 强度太高；把强度降到 60–80 再比一次（高光 taper 只收 25%） |
| 改了代码没生效 | 前端每次请求读盘，刷新即可；`server.py` 改动要重启进程 |
