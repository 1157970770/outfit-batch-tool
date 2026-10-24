import pathlib

p = pathlib.Path(r"D:\批量视频任务设计区\app\main.py")
s = p.read_text(encoding="utf-8")
i = s.index("    def _open_settings")
print(repr(s[i:i + 400]))
print("---- 之后 ----")
print(s[i + 400:i + 600])
