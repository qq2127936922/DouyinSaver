"""真机自测：把真实目录的「目录结构」复制到临时目录，跑一遍 reclassify 看归类结果。"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pipeline import Pipeline, is_work_dir  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "抖音收藏助手" / "downloads"
tmp = Path(tempfile.mkdtemp(prefix="reclass_"))
dst = tmp / "downloads"
dst.mkdir(parents=True)

n = 0
for d1 in sorted(SRC.iterdir()):
    if not d1.is_dir() or d1.name.startswith("."):
        continue
    for d2 in sorted(d1.iterdir()):
        if not d2.is_dir():
            continue
        if is_work_dir(d2):
            (dst / d1.name / d2.name).mkdir(parents=True, exist_ok=True)
            (dst / d1.name / d2.name / f"{d2.name}.mp4").write_bytes(b"x" * 16)
            n += 1
        else:
            for d3 in sorted(d2.iterdir()):
                if is_work_dir(d3):
                    (dst / d1.name / d2.name / d3.name).mkdir(parents=True, exist_ok=True)
                    (dst / d1.name / d2.name / d3.name / f"{d3.name}.mp4").write_bytes(b"x" * 16)
                    n += 1
print(f"造了 {n} 个作品目录 → {dst}")

cfg = {
    "download_dir": str(dst),
    "profile_dir": str(ROOT / ".probe_browser"),
    "duplicate": "skip",
    "baidu": {"enabled": False, "mode": "off"},
    "images": {"save_images": True, "save_bgm": True, "make_video": True, "image_seconds": 2.6},
    "organize": {"by_collect_folder": True, "fallback_folder": "收藏"},
}

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.1f}s] {m}", flush=True)  # noqa: E731
p = Pipeline(cfg=cfg, log=log)
res = p.reclassify(headless=True)
print("结果:", json.dumps(res, ensure_ascii=False))

print("\n== 整理后的目录 ==")
for d1 in sorted(dst.iterdir()):
    cnt = sum(1 for d2 in d1.iterdir() if d2.is_dir())
    sub = sum(1 for d2 in d1.iterdir() if d2.is_dir() and not is_work_dir(d2))
    print(f"  {d1.name}: {cnt} 个作品" + ("" if sub == cnt else f"（含 {cnt-sub} 个作者层）"))
print(f"\n临时目录保留在 {tmp}（确认后手动删）")
