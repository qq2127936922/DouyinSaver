"""对真实安装目录跑一次「收藏 + 点赞」同步（新布局）。

用法：
    python run_sync_real.py            # 增量（每个收藏夹取最新一页）
    python run_sync_real.py --full     # 全量翻到底
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pipeline import Pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
cfg = json.loads((ROOT / "抖音收藏助手/config.json").read_text(encoding="utf-8"))
FULL = "--full" in sys.argv
RECONCILE = "--reconcile" in sys.argv
# 从源码跑时 config.ROOT 是项目目录，登录态在安装目录那边，得指过去
cfg["profile_dir"] = str(ROOT / "抖音收藏助手/.browser")

print("下载目录:", cfg["download_dir"], flush=True)
print("范围:", (cfg.get("auto_sync") or {}).get("scope"), "| 全量:", FULL, flush=True)

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:6.1f}s] {m}", flush=True)  # noqa: E731
p = Pipeline(cfg=cfg, log=log, upload_kind="collect")
print("网盘根目录:", p.pan.remote_root(), flush=True)

got = p.sync_favorites(headless=True, scope="both", full=FULL, reconcile=RECONCILE)
print("\n结果:", json.dumps(got, ensure_ascii=False), flush=True)

root = Path(cfg["download_dir"])
works = [d for d in root.glob("*/*") if d.is_dir() and d.name[-6:].isdigit()]
print(f"\n本地共 {len(works)} 个作品，顶层分布：")
from collections import Counter  # noqa: E402

for name, n in Counter(x.parent.name for x in works).most_common():
    depths = {len(x.relative_to(root).parts) for x in works if x.parent.name == name}
    print(f"   {name:<10} {n:>3} 个（层级 {sorted(depths)}）")
