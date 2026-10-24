import pathlib

s = pathlib.Path(r"D:\批量视频任务设计区\app\main.py").read_text(encoding="utf-8")
for pat in ("output_root", '"api_key"', "EMBEDDED_KEY", "SettingsDialog", "save_config",
            "_open_settings", '"manifest"', "cos_enabled", "cos_bucket"):
    print(f"{pat:16s} {s.count(pat)}")
print("行数", s.count("\n") + 1)
