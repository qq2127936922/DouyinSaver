"""在真实安装目录上跑一次「按收藏夹整理」（只移动文件，不下载）。

用法：
    python run_reclass.py            # 真正执行（有 3 天内的缓存就直接用）
    python run_reclass.py --dry      # 只看会怎么动，不动文件
    python run_reclass.py --fresh    # 忽略缓存，重新扫一遍收藏夹
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pipeline import Pipeline, is_work_dir  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "抖音收藏助手"
PROFILE = ROOT / ".probe_browser"
DRY = "--dry" in sys.argv
FRESH = "--fresh" in sys.argv

cfg = json.loads((INSTALL / "config.json").read_text(encoding="utf-8"))
download = Path(cfg.get("download_dir") or INSTALL / "downloads")
print("下载目录:", download, download.is_dir())
print("备注：profile 用测试副本（安装目录 .browser 可能被运行中的程序占用）")

works_before = []
if download.is_dir():
    for d1 in sorted(d for d in download.iterdir() if d.is_dir()):
        for d2 in sorted(d for d in d1.iterdir() if d.is_dir()):
            if is_work_dir(d2):
                works_before.append(f"{d1.name}/{d2.name}")
            else:
                for d3 in sorted(d for d in d2.iterdir() if d.is_dir()):
                    if is_work_dir(d3):
                        works_before.append(f"{d1.name}/{d2.name}/{d3.name}")
print(f"整理前共 {len(works_before)} 个作品")
(ROOT / "before_reclass.txt").write_text("\n".join(works_before), encoding="utf-8")

if DRY:
    print("dry 模式：只读，不移动")
    sys.exit(0)

cfg["profile_dir"] = str(PROFILE)
cfg["organize"] = {"by_collect_folder": True, "fallback_folder": "收藏"}
# 老配置里应用名是空的，但网盘上文件其实在 /apps/抖音备份 下面，补上才好一起整理
b = dict(cfg.get("baidu") or {})
if not (b.get("app_name") or "").strip():
    b["app_name"] = "抖音备份"
    cfg["baidu"] = b
print("网盘目录:", f"/apps/{b.get('app_name')}" + ("" if b.get("remote_dir") in ("", b.get("app_name")) else f"/{b.get('remote_dir')}"))

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.1f}s] {m}", flush=True)  # noqa: E731
p = Pipeline(cfg=cfg, log=log)
res = p.reclassify(headless=True, use_cache=not FRESH)
print("结果:", json.dumps(res, ensure_ascii=False))

works_after = []
for d1 in sorted(d for d in download.iterdir() if d.is_dir()):
    for d2 in sorted(d for d in d1.iterdir() if d.is_dir()):
        if is_work_dir(d2):
            works_after.append(f"{d1.name}/{d2.name}")
        else:
            for d3 in sorted(d for d in d2.iterdir() if d.is_dir()):
                if is_work_dir(d3):
                    works_after.append(f"{d1.name}/{d2.name}/{d3.name}")
(ROOT / "after_reclass.txt").write_text("\n".join(works_after), encoding="utf-8")
print(f"整理后共 {len(works_after)} 个作品（移动 {res['moved']} 个）")
print("清单已存 before_reclass.txt / after_reclass.txt，需要回滚照单搬回即可")
