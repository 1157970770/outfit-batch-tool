"""远程配置链路测试：服务端 config.json → 激活 → 客户端拿到 → 映射成 cfg。"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(r"D:\批量视频任务设计区")
TMP = Path(tempfile.mkdtemp(prefix="rc-"))
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["LICENSE_SECRET"] = "remote-config-test"
os.environ["LICENSE_PASSWORDS"] = str(TMP / "passwords.json")
os.environ["LICENSE_CONFIG"] = str(TMP / "config.json")

sys.path.insert(0, str(ROOT))

(TMP / "passwords.json").write_text(json.dumps({"passwords": {
    "RC-TEST": {"note": "测试", "expires": "2099-12-31", "revoked": False}}}), encoding="utf-8")
(TMP / "config.json").write_text(json.dumps({
    "rh_api_key": "rh-key-from-server",
    "manifest_url": "https://cdn.example.com/manifest.json",
    "cos": {"enabled": True, "bucket": "b-1463907213", "region": "ap-guangzhou",
            "prefix": "prod", "domain": ""},
    "notice": "本周模板已更新",
}), encoding="utf-8")

from app import config as cfgmod  # noqa: E402
from app import license as lic  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


port = free_port()
server = f"http://127.0.0.1:{port}"
proc = subprocess.Popen([sys.executable, str(ROOT / "server" / "license_api.py"), "--serve", str(port)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())
for _ in range(50):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            break
    except OSError:
        time.sleep(0.1)

print("激活前 config:", lic.get_config())
lic.activate(server, "RC-TEST")
remote = lic.get_config()
print("下发字段:", sorted(remote))

cfg = cfgmod.load()
print("RH_API_KEY:", cfg["RH_API_KEY"])
print("MANIFEST:", cfg["MANIFEST"])
print("COS_ENABLED:", cfg["COS_ENABLED"], "| BUCKET:", cfg["COS_BUCKET"], "| PREFIX:", cfg["COS_PREFIX"])
print("NOTICE:", cfg["NOTICE"])

# 缓存文件里不能有明文
cache_file = Path(os.environ["APPDATA"]) / "RHOutfitTool" / "license.json"
raw = cache_file.read_text(encoding="utf-8")
print("缓存文件前缀:", raw[:10], "| 含明文 rh-key:", "rh-key-from-server" in raw)

ok = (cfg["RH_API_KEY"] == "rh-key-from-server" and cfg["COS_ENABLED"] == "true"
      and cfg["MANIFEST"].endswith("manifest.json") and "rh-key-from-server" not in raw)
proc.terminate()
print("OK 远程配置链路通过" if ok else "FAIL")
