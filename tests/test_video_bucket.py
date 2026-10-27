"""真机验证：没有收藏夹的作品放进「视频」文件夹（只下 1 条）。"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import DEFAULTS, _merge  # noqa: E402
from core.douyin import DouyinBrowser  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="dsy_video_"))
cfg = _merge(json.loads(json.dumps(DEFAULTS)), {
    "download_dir": str(tmp),
    "duplicate": "skip",
    "baidu": {"enabled": False, "mode": "off"},
    "auto_sync": {"enabled": False},
})
cfg["profile_dir"] = str(ROOT / ".probe_browser")

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.1f}s] {m}", flush=True)  # noqa: E731
p = Pipeline(cfg=cfg, log=log)
print("落盘根:", tmp, flush=True)

picked = {"a": None}


def on_items(folder, awemes):
    if picked["a"] is None and awemes:
        picked["a"] = awemes[0]


with DouyinBrowser(ROOT / ".probe_browser", headless=True, log=log) as br:
    br.collect_folders(full=False, on_items=on_items)

aweme = picked["a"]
assert aweme, "没取到作品"
ok = p.save_aweme(aweme, folder=None)      # 没有收藏夹 → 应该进「视频」
print("save_aweme →", ok, flush=True)

dirs = [d for d in tmp.rglob("*") if d.is_dir() and d.name[-6:].isdigit()]
print("\n落盘结果：")
for d in dirs:
    print("  ", d.relative_to(tmp).as_posix())
    print("     文件:", [f.name[-12:] for f in d.iterdir()])
rel = dirs[0].relative_to(tmp).parts if dirs else ()
print("\n顶层目录 =", rel[0] if rel else "（无）", "| 层级 =", len(rel))

import shutil  # noqa: E402

shutil.rmtree(tmp, ignore_errors=True)
print("清理完成")
