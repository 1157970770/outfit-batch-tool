# 换装批量生产器 · 测试版 v0.1

**这是一个文件夹版程序，要整个文件夹一起拷，不能只拷 exe。**

```
换装批量生产器\
├── 换装批量生产器.exe   ← 双击这个
└── _internal\           ← 依赖文件，少了它 exe 起不来
```

分发时直接把 `换装批量生产器-v0.1.zip` 发给别人，解压后双击 exe。
一个界面，两个页面：**小批量试跑**（随机 3 条）和**大批量生产**（5 组 × 100 条）。

![界面](docs/ui-preview.png)

## 第一次使用（三步）

**① 填 `配置.ini`**（exe 同目录，首次打开程序会自动生成）

用记事本打开，只需要改这两行：

```ini
RH_API_KEY=你的32位Key
MANIFEST=https://你的CDN/manifest.json
```

其余（输出目录、COS 归档）都有默认值，不用动。改完保存，回软件点右上角 **重载配置**。

> 界面上**没有任何配置输入框**——Key、清单、存储全部在后台文件里，客户拿到程序只需要填一个文件。
> 不知道怎么填？点右上角 **配置文件** 会直接用记事本打开它。

**② 选服装图**：一张无头模特图（服装 + 上身效果）

**③ 填批次号** → 点「试跑 3 条」，确认脸、衣服、脖子衔接都没问题，再去「大批量生产」放量

## 输出长什么样

```
我的文档/换装输出/
└── 20261024A/            ← 你填的批次号（重名自动变 20261024A-2）
    ├── 001.mp4           ← 模板 001 的换装结果
    ├── 002.mp4
    ├── report.csv        ← 每条的状态/耗时/扣费/大小，可直接给财务
    ├── batch.json        ← 本次用的提示词原文 + taskId，可追溯
    └── log.txt           ← 运行日志
```

## 界面上的几件事

| 位置 | 说明 |
| --- | --- |
| 右上角「配置文件」 | 用记事本打开 `配置.ini`（找不到就自动生成） |
| 右上角「重载配置」 | 改完配置点一下生效，不用重启 |
| 模板下拉 | 第 1–5 组（001–100 … 401–500），或「随机 100 条」 |
| 并发 | 3（稳）/10/50/100；账号有并发上限，失败多就调回 3 |
| 提示词 | 可随意改，改完即用；原文会存进 `batch.json` |
| 暂停 | 暂停后不会提交新任务，正在跑的几条会跑完 |
| 已完成再点开始 | 同批次号会新建 `-2` 目录，不覆盖历史 |

**失败**：自动跳过并记在 `report.csv` 的「错误」列，整批不会中断。把失败编号记下来，单独再跑一次即可。

## 配置项一览（`配置.ini`）

| 键 | 必需 | 说明 |
| --- | --- | --- |
| `RH_API_KEY` | ✅ | RunningHub 32 位 Key，企业级共享 |
| `MANIFEST` | ✅ | 模板清单地址（https 或本地路径） |
| `OUTPUT_ROOT` | — | 留空 = exe 同目录 `输出\` |
| `COS_ENABLED` | — | `true` 则成品自动上传 COS |
| `COS_BUCKET` / `COS_REGION` / `COS_PREFIX` / `COS_DOMAIN` | — | 启用 COS 时填 |
| （COS 密钥） | — | **不写进配置文件**，走环境变量 `COS_SECRET_ID/KEY`、`~/.cos/credentials.json` 或 tccli 登录态 |

## 开发者：重新打包

```bash
pip install PySide6 pyinstaller
python3 -m PyInstaller --noconfirm --clean --onedir --windowed \
  --name "换装批量生产器" --paths "." run_app.py
# 产物：dist\换装批量生产器\{换装批量生产器.exe, _internal\}
# 分发前压成 zip：Compress-Archive dist\换装批量生产器 dist\换装批量生产器-v0.1.zip
```

**别用 `--onefile`**：单文件版每次启动都要把几百 MB 依赖解压到 `%TEMP%`，
在 TEMP 被管控（或被杀软实时扫描）的机器上会直接弹 `Could not create temporary directory!`
然后打不开。文件夹版不解压、启动更快，也更好排查。

- 内置 Key：改 `app/main.py` 顶部的 `EMBEDDED_KEY`
- 无界面试跑：`python3 tests/test_core.py`（核心引擎）、`python3 tests/test_ui.py`（界面集成）
- 模板清单生成：`python3 tools/build_manifest.py --src D:\templates --base-url https://cdn.xxx.com/tpl --out manifest.json --posters posters`

## 已知限制（v0.1）

| 限制 | 说明 |
| --- | --- |
| Key 明文内置 | 拿到 exe 的人都能用你的余额，正式分发前建议加激活码或轻中转 |
| COS 密钥明文 | 「设置」里的 SecretKey 明文存 `%APPDATA%\RHOutfitTool\config.json`；多人共用机器请改用环境变量（`COS_SECRET_ID`/`COS_SECRET_KEY`） |
| 必须整个文件夹一起拷 | 只拷 exe 会缺 `_internal`，起不来；已用 zip 分发 |
| 无界面自动更新 | 换模板只改 manifest，无需发版；换程序要重新发 exe |
| 没有缩略图预览 | 按设计：大批量生产不需要辨认单个模板，只要分组编号 |
