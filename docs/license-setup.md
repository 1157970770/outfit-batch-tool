# 授权（密码）方案：最简单可用版

> 目标：拿到 exe 的人，**必须输入你分配的密码**才能用。密码可单独发给某人、可单独停用、换电脑不能复用。

## 组成

| 文件 | 作用 |
| --- | --- |
| `server/license_api.py` | 授权服务，单文件零依赖；可本地跑，也可直接传腾讯云函数 |
| `server/passwords.example.json` | 密码表模板，改成 `passwords.json` 即生效 |
| `app/license.py` | 客户端：密码 → token、本机缓存、机器码绑定 |
| `app/startup.py` | 启动门禁：没授权就弹输入框，3 次失败退出 |
| `tests/test_license.py` | 10 项自动化验证（含换机器、停用、过期） |

开关只有一个：`配置.ini` 里的 `LICENSE_SERVER`。**留空 = 不校验**（自己用不受影响），填了就强制校验。

## 一、部署服务端（二选一）

### 方式 A：腾讯云函数 SCF（推荐，国内快、按量几乎免费）

1. 新建函数 → 运行环境 Python 3.9+ → 上传 `license_api.py`
2. 执行入口填：`license_api.main_handler`
3. 环境变量：
   - `LICENSE_SECRET` = 一串随机字符（**必须改，越乱越好**）
   - `LICENSE_TTL_DAYS` = `14`（token 有效期）
   - `LICENSE_PASSWORDS` = `/var/user/passwords.json`（把密码表一起传上去）
4. 触发器：API 网关，路径任意（`/license` 即可），方法 POST
5. 拿到公网地址，形如 `https://xxxx.apigw.tencentcs.com/license`

### 方式 B：任意一台能跑的机器

```bash
set LICENSE_SECRET=换成一串随机字符
python3 server/license_api.py --serve 8099
```

内网用 `http://内网IP:8099`，公网自己配反代 + HTTPS。

## 二、发密码

编辑 `passwords.json`，一个用户一行：

```json
{
  "passwords": {
    "RH-2026-001": { "note": "张三 / 设计部", "expires": "2027-12-31", "revoked": false },
    "RH-2026-002": { "note": "李四 / 外包",   "expires": "2026-12-31", "revoked": false }
  }
}
```

- `expires`：到期后该密码失效（`2026-12-31` 或 `2026-12-31 23:59`）
- `revoked: true`：立刻停用（下一次启动校验就会拒绝）
- `machine`：留空即可；填了机器码就锁定那台机器

改完**重新部署一次函数**（或重启本地服务）就生效，不需要数据库。

## 三、客户端怎么用

`配置.ini` 里加一行：

```ini
LICENSE_SERVER=https://xxxx.apigw.tencentcs.com/license
```

启动软件 → 弹「需要授权」→ 输入密码 → `授权成功` → 进主界面。
之后 14 天内不再问；换电脑会要求重新输入（token 绑定机器码）。

## 四、已验证的行为（`python3 tests/test_license.py`）

```
✓ 未激活时拒绝          ✓ 错误密码被拒
✓ 停用密码被拒          ✓ 到期密码被拒
✓ 正确密码激活后可用    ✓ token 已存本地
✓ 缓存里不含明文密码    ✓ token 绑定机器（换机器失败）
✓ 停用后立即拒绝        ✓ 停用后缓存被清空
```

## 五、这套方案的边界（先说清楚）

| 限制 | 影响 | 缓解 |
| --- | --- | --- |
| 服务端不存状态 | 吊销要等客户端下次联网校验 | token 有效期设短（如 7 天），吊销最多延迟这么久 |
| 不能防逆向 | 有人改 exe 跳过校验 | 只防"随手转发"，防不了专业破解 |
| 断网可用 | 已激活的机器断网仍能跑到 token 到期 | 想强依赖网络就把 `LICENSE_TTL_DAYS` 设 1 |
| 密码明文存在服务端 | 泄露 = 所有人都能用 | 别提交 `passwords.json` 到 Git；只放函数里 |
| 没有用量统计 | 不知道谁跑了多少条 | 需要的话，在 `/activate` 里加一行写 COS 或日志 |

## 六、和"内置 RH Key"的关系

两者是**两层**，互不替代：

- **RH Key**（`配置.ini` 的 `RH_API_KEY`）：谁出钱、能调多少次；
- **授权密码**：谁能打开这个软件。

分发给别人时两层都开：密码控制"谁能用"，RH Key 决定"每次跑花谁的钱"。
