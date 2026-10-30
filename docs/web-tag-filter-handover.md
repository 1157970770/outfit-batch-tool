# 本地网页生产工具：标签筛选改造交接

日期：2026-10-30（15:20 复核后更新）  
分支：`feature/web-lan`  
状态：**标签筛选已在本地跑通；改动仍未提交。未运行真实 Wan 任务，也未调用 COS 写入。**

## 需求与数据

生产前端按「标签清单1版.xlsx」筛选视频，支持用户选标签后手选或自动推荐。

- 工作簿原件：`C:\Users\EDY\Desktop\标签清单1版.xlsx`，只读加载，不修改、不覆盖。
- 视频目录：`D:\星伙素材\磊哥外拍视频（总）`（1278 个 mp4）。
- 工作簿 1210 行视频记录，`状态` 全部为「待上传」；目录另外 68 个未列入清单的视频是用户主动剔除项，网页候选集应排除（已能自动算出并显示）。
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
- 环境说明：主机 Python 需 `openpyxl`（读工作簿）与 `Pillow`（仅测试造图）。本轮两者均已安装，Pillow 需从本地 wheel 安装（直接 `pip install pillow` 在本环境会被中断）。

## 剩余事项

1. 用浏览器打开网页确认筛选 UI 观感（本轮只做了接口级校验，未截图核对）。
2. 1210 条视频需要批量上传到 COS，且上传器要把**工作簿模板ID/文件名写进对象名或 manifest**，否则生成功能仍不可用（当前 `source_ready_templates=0`）。
3. 确认后只提交本次 Web 标签筛选相关改动，不要暂存 `README.md`、`docs/handover.md`、`docs/换装批量生产器-项目交接手册.docx`。

## 当前工作区注意

未提交的本次改动：`app/core.py`、`app/web/server.py`、`app/web/settings.py`、`app/web/templates.py`、`app/web/static/app.js`、`app/web/static/index.html`、`app/web/static/tag-filter.css`、`tests/test_web_tags.py`、`requirements.txt`、`docs/web-lan.md`、`docs/web-tag-filter-handover.md`。  
另有原先的未提交用户文件：`README.md`、`docs/handover.md`、`docs/换装批量生产器-项目交接手册.docx`；不要覆盖或提交它们。

本地网页服务当前没有监听 8765 端口（本轮验证用的是 8771–8774 临时端口，已全部停止）；停止时不要误报成正在运行。所有实际 Wan API/COS 凭据只在主机本地核对，不要打印或写入报告。
