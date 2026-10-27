# 离线联调（不花钱）

`mock_dashscope.py` 在本机起一个假的阿里云百炼（万相 3.0），覆盖
`提交 → 查询 → 下载`，用来验证官方请求体、状态机、断点续跑和批量并发，
不消耗余额、不依赖外网。

```bash
# 终端 1：起假服务（端口随意）
python3 tests/mock_dashscope.py 18090

# 终端 2：让客户端指向它
set WAN_BASE_URL=http://127.0.0.1:18090/api/v1
set WAN_API_KEY=demo-key-0123456789abcdef
python3 wan3_r2v.py run --tasks tasks.example.json --limit 1 --interval 0.2 --timeout 20
```

预期：`out/<id>.mp4` 落盘、`out/state.json` 记录 `task_id`、`usage`
（假服务返回 `duration=14 / output_video_duration=7 / SR=720`）。

> `WAN_BASE_URL` 也可指向自建代理或抓包网关；不设置时走
> `https://dashscope.aliyuncs.com/api/v1`。
>
> 旧链路回滚：`python3 tests/mock_runninghub.py 18080` +
> `--provider runninghub --base-url http://127.0.0.1:18080/openapi/v2`
> + `RUNNINGHUB_API_KEY`。

## 测试套件

```bash
python3 tests/test_wan_api.py     # 官方体映射 / 约束 / 错误归类 / 计费 / 源图上传
python3 tests/test_storage.py     # 假 COS 客户端 + 本机 HTTP 服务
python3 tests/test_core.py        # 核心引擎（第二遍验证归档到"本地对象存储"）
python3 tests/test_ui.py          # 界面集成（离屏 + 假百炼，需 PySide6）
python3 tests/test_remote_config.py  # 服务端 config.json → 激活 → 客户端映射
python3 tests/test_prompts.py     # 提示词库
python3 demo.py --count 5         # 一键离线演练
```

`test_wan_api.py` 不联网，覆盖：edit/reference 媒体顺序、`duration` 整数、
`ratio/prompt_extend` 新字段、官方硬约束、错误分类、`usage` 计费、源图 Key/签名。
