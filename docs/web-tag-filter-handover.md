# 本地网页生产工具：标签筛选改造交接

日期：2026-10-30（16:40 换用新清单）  
分支：`feature/web-lan`  
状态：**功能已提交（`cfd420e`）；已适配「新标签清单.xlsx」但适配改动尚未提交；服务在 8765 上跑的是旧清单配置，需重启生效。未运行真实 Wan 任务，也未调用 COS 写入。**

## 需求与数据

生产前端按「标签清单1版.xlsx」筛选视频，支持用户选标签后手选或自动推荐。

- 工作簿原件：`C:\Users\EDY\Desktop\标签清单1版.xlsx`，只读加载，不修改、不覆盖。
- 视频目录：`D:\星伙素材\磊哥外拍视频（总）`（1278 个 mp4）。
- 工作簿 1210 行视频记录，`状态` 全部为「待上传」；目录另外 68 个未列入清单的视频是用户主动剔除项，网页候选集应排除（已能自动算出并显示）。

> **2026-10-30 16:40 更新（已换用新清单）**：生产清单一词现在指桌面 **`新标签清单.xlsx`**（1339 行 = 原 1210 条 + 新增 129 条，ID 1279–1411；标签字典与旧版完全一致，旧 1210 条的标签无任何丢失）。目录已增至 1344 项（1343 个 mp4 + 1 个并入记录 csv）。**没打标签的行 = 废弃素材**（用户确认），共 76 行，静默排除；未列入清单的另有 **4 个**。账目自洽：1263 可筛选 + 76 废弃 = 1339 清单行，1343 − 1339 = 4 未入清单。下文涉及「1210」的段落是当时的记录，保留原样。
- 四类标签：景别、适合季节、场景、姿势。相同类别内 OR，不同类别之间 AND。

## ⚠️ 关键复核结论（2026-10-30）

原计划假设"工作簿 1210 个 ID 全部匹配 manifest"，**实测不成立，且比预想更危险**：

| | COS manifest（`manifest_url`） | 标签清单1版.xlsx |
|---|---|---|
| 条目数 | **108** | **1210** |
| ID 区间 | 001–108 | 001–1210 |
| 名称 | 原始名，如「10月8日(1)」 | 重命名后 `0001.mp4`…`1210.mp4` |
| 状态 | 已在 `input/tpl/` | 全部「待上传」 |

- 108 条是**早期已上云端的测试清单，未来会删除**；本地 1210 条才是真实生产清单。
- 1102 个清单 ID 不在 manifest。旧实现要求"清单 ID ⊆ manifest ID"，命中即整体拒绝服务，实测 `templates=0`、网页无任何候选。
- 更严重：剩下 108 个"能对上"的是**假匹配**。manifest `001` =「10月8日(1)」5.57MB，工作簿 `001` = `0001.mp4` 8.59MB/7.5s，只是数字区间碰巧重叠。旧校验用集合包含判断，发现不了，一旦放行会**拿错视频换装**。
- 按文件大小交叉只能对上 79/108，进一步确认两套不是同一编号体系。
- `tools/upload_templates.py` 是旧分组目录的上传器（按枚举顺序编号、`name` 取原文件名），与这份工作簿无关；1210 条目前**还没有对应的批量上传器**。

## 已实现

- `app/web/templates.py`：**以工作簿为生产清单重建候选集**。工作簿仍是唯一候选来源；manifest 降级为"已上传索引"，只在**文件名/相对路径/指纹 + 大小一致**且唯一时才补 `url/key`，**绝不按模板 ID 匹配**。新增 `ready`、`upload_state`、`unlisted_files`（自动统计清单外文件数）。
- `app/web/settings.py`：工作簿存在时不再依赖 manifest 加载成功；manifest 失败只降级为提示，不阻断筛选。
- `app/web/server.py`：`/api/bootstrap` 增加 `source_ready_templates`、`unlisted_files`、`manifest_warning`；提交时若所选视频没有云端地址，返回明确中文提示；预览同样给出"尚未上传"而不是 502；`reload_templates` 同步新语义。
- `app/core.py`：`load_manifest` 保留 `name/shot/size_mb/relative_path/filename/fingerprint`（有值才写，旧行为不变），否则大小校验永远无法通过。
- `app/web/static/app.js`：状态栏显示"N 个已标注视频场景（X 个可生成）"；未上传时置顶提示、筛选摘要标注"均未上传云端"、提交按钮禁用并说明原因。
- `app/web/static/index.html`、`static/tag-filter.css`：四类标签筛选 UI（上一轮已完成）。
- `tests/test_web_tags.py`：按新语义重写，覆盖工作簿为准、文件名+大小对齐、同 ID 不同文件必须拒绝、无 manifest 也能筛选、重复 ID / 字典外标签阻断、68 类排除。
- `requirements.txt`：补 `openpyxl>=3.1`（原先漏写，生产机缺包会直接报错）。

## 已验证

- `python tests/test_web_tags.py`：通过。
- `python tests/test_web.py`：通过（本地 mock，不产生 Wan 费用）。**该测试抓出一个回归**：manifest 正常加载时 `templates_error` 被误写成"服务端没有配置模板清单"，导致提交恒返回 503，已修复。
- `python tests/test_core.py`：通过（3/3 成功，含 COS 归档路径）。
- `python tests/test_web_auth.py`、`tests/test_template_catalog.py`、`tests/test_remote_config.py`：通过。
- `node --check app/web/static/app.js`：通过。
- 真实配置加载：1210 条候选、`source_ready_templates=0`、`unlisted_files=68`，无错误。
- 起本地服务（临时账号，未提交任何任务）实测：`/api/login` 200、`/api/bootstrap` 返回 `templates_count=1210 / tag_filter_enabled=true / unlisted_files=68`、`/api/templates` 返回 1210 条、`/api/templates/001/preview` 返回 404「该视频尚未上传到云端，暂无预览」。
- 原工作簿未被修改；未提交任务，未上传 COS。
- 16:00–16:04 本机浏览器实际访问：`/`、`/static/app.js`、`/static/tag-filter.css`、`/api/me`、`/api/bootstrap`、`/api/templates` 全部 200，无 4xx/5xx，标签筛选 UI 已人工看过。
- 15:58 跑过一次离线演练（`demo.py`，mock，不花钱）：3 条任务瞬时成功，桌面侧链路正常。
- 环境说明：主机 Python 需 `openpyxl`（读工作簿）与 `Pillow`（仅测试造图）。本轮两者均已安装，Pillow 需从本地 wheel 安装（直接 `pip install pillow` 在本环境会被中断）。

## 提交记录

- `cfd420e feat(web): filter templates by tag workbook` —— 本次功能全量：`app/core.py`、`app/web/{server,settings,templates}.py`、`static/{app.js,index.html,tag-filter.css}`、`tests/test_web_tags.py`、`requirements.txt`、`docs/web-lan.md`。
- `21c7d6a docs: add project handover guide` —— 交接文档与 README（未混入功能改动，两者保持独立提交）。

## 换用新清单（2026-10-30 16:40）

- 生产清单改为 `C:\Users\EDY\Desktop\新标签清单.xlsx`：`server/web.json` 新增 `"tag_workbook"` 显式指向它，代码里的桌面默认路径也同步改成新文件名（`app/web/settings.py`）。旧文件 `标签清单1版.xlsx` 未修改、未删除。
- 新旧对比：工作表/表头/标签字典（20 条）**完全一致**；行数 1210 → 1339（新增 129 条，ID 1279–1411）；原有 1210 条的标签**零丢失**；"标签字典外标签" 0 处；状态仍全为「待上传」。
- 新增的 129 条里 53 条已打标、**76 条未打标**。用户确认**未打标 = 废弃素材**，因此按"静默排除"处理：不进候选集、不在页面提示、不当作待办，只在 `tag_workbook_info["untagged_skipped"]` 里留个数字供排查。仅当整张表都没有可用行时才报错。重复 ID、字典外标签、缺路径仍然硬阻断。
- 顺带修掉一个统计口径问题：未打标行原先会被算进"清单外文件"，现在 `unlisted_files` 只统计"目录里有、清单里没有"的视频（当前 **4** 个）。
- 加载结果：`workbook_rows=1263`、`untagged_skipped=76`、`unlisted_files=4`、`source_ready_templates=0`。
- 测试：`tests/test_web_tags.py` 增加"未打标行被跳过且不计入清单外""全部未打标时明确报错"两个用例；`test_web_tags / test_web / test_web_auth / test_template_catalog` 全通过。
- ⚠️ **改的是启动时读取的配置，正在运行的服务（8765）不会自动生效**，需要关掉控制台重开一次 `启动网页服务.bat`。

## 剩余事项

1. 1263 条已打标视频需要批量上传到 COS，且上传器要把**工作簿模板ID/文件名写进对象名或 manifest**，否则生成功能仍不可用（当前 `source_ready_templates=0`）。这是唯一阻塞"能出片"的事项。
2. 108 条旧的测试清单（`input/tpl/` + `manifest.json`）按计划删除时，注意不要连带删掉仍需保留的桶内其他对象；删除前先确认 `server/config.json` 的 `manifest_url` 指向哪里。
3. 可选清理：76 条废弃行的视频文件仍留在 `D:\星伙素材\磊哥外拍视频（总）`，程序已不引用它们；要腾空间可以自行删除（**不要由程序代删**，避免误删清单外素材）。

## 当前工作区注意

功能改动已全部提交；本文件与 `docs/web-lan.md`、`docs/handover.md` 属于状态同步更新，`app/web/{templates,settings}.py`、`app/web/static/app.js`、`app/web/server.py`、`tests/test_web_tags.py` 属于"新清单适配"改动，均**尚未提交**。  
`README.md`、`docs/handover.md`、`docs/换装批量生产器-项目交接手册.docx` 已在 `21c7d6a` 入库。  
网页服务当前**正在监听 8765**，但跑的是旧清单配置，重启后才会切到 `新标签清单.xlsx`。所有实际 Wan API/COS 凭据只在主机本地核对，不要打印或写入报告。
