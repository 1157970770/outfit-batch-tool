# 离线联调（不花钱）

`mock_runninghub.py` 在本机起一个假的 RunningHub，覆盖 `提交 → 轮询 → 下载 → 素材上传` 四个接口，
用来验证 `wan3_r2v.py` 的请求格式、断点续跑、批量并发，不消耗余额、不依赖外网。

```bash
# 终端 1：起假服务（端口随意）
python3 tests/mock_runninghub.py 18080

# 终端 2：让客户端指向它
set RH_BASE_URL=http://127.0.0.1:18080/openapi/v2
set RUNNINGHUB_API_KEY=testkey-0123456789abcdef
python3 wan3_r2v.py run --tasks tasks.example.json --limit 1 --interval 0.2 --timeout 20
```

预期：`out/<id>.mp4` 落盘、`out/state.json` 记录 `taskId` 与 `consumeMoney`（假服务固定返回 0.42）。

> `RH_BASE_URL` 也可指向自建代理或抓包网关；不设置时走 `https://www.runninghub.cn/openapi/v2`。

## 存储层（对象存储）测试

```bash
python3 tests/test_storage.py    # 假 COS 客户端 + 本机 HTTP 服务
python3 tests/test_core.py       # 核心引擎（第二遍会验证归档到"本地对象存储"）
```

`test_storage.py` 不联网、不需要 `cos-python-sdk-v5`，覆盖：Key 规则、内容指纹、
本地后端读写/越界拦截、小对象上传、大文件走分片、5xx 自动重试、配置校验，
以及转存的 成功 / 幂等跳过 / 404 / 403 过期 / 200 HTML 错误页 / 批量并发。

> 真连 COS 用 `python3 scripts/cos_check.py`（会真写一个几十字节的探针对象）。
