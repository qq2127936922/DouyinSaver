"""真机验证：scope=both 只下载「既收藏又点赞」的作品。"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import _merge  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
install = json.loads((ROOT / "抖音收藏助手/config.json").read_text(encoding="utf-8"))
tmp = Path(tempfile.mkdtemp(prefix="dsy_both_"))
cfg = _merge(install, {
    "download_dir": str(tmp),
    "duplicate": "skip",
    "baidu": {"enabled": False, "mode": "off"},
    "auto_sync": {"enabled": False, "scope": "both"},
})
cfg["profile_dir"] = str(ROOT / ".probe_browser")

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.1f}s] {m}", flush=True)  # noqa: E731
p = Pipeline(cfg=cfg, log=log)
got = p.sync_favorites(headless=True, scope="both", full=False)
print("\n结果:", json.dumps(got, ensure_ascii=False))
works = [d for d in tmp.rglob("*") if d.is_dir() and d.name[-6:].isdigit()]
print(f"实际落盘 {len(works)} 个作品：")
for w in works:
    print("  ", w.relative_to(tmp).as_posix())
print("（每个都应该是「既收藏又点赞」的；只收藏没点赞的不该出现）")
