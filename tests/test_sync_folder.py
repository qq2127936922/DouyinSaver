"""真机验证：新备份是否落到「收藏夹/作品」下面（只下 2 条）。"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import downloader as dl  # noqa: E402
from core.config import DEFAULTS, _merge  # noqa: E402
from core.douyin import DouyinBrowser  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="dsy_folder_"))
cfg = _merge(json.loads(json.dumps(DEFAULTS)), {
    "download_dir": str(tmp),
    "duplicate": "skip",
    "headless": True,
    "baidu": {"enabled": False, "mode": "copy"},
    "auto_sync": {"enabled": False},
})
cfg["profile_dir"] = str(ROOT / ".probe_browser")
print("临时下载目录:", tmp)

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.1f}s] {m}", flush=True)  # noqa: E731
p = Pipeline(cfg=cfg, log=log)

saved = {"n": 0}


def on_items(folder, awemes):
    for aweme in awemes:
        if saved["n"] >= 2:
            break
        title = dl.aweme_meta(aweme)["title"][:32]
        try:
            ok = p.save_aweme(aweme, folder=folder["name"])
            print(f"  保存 [{folder['name']}] {title} → {ok}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print("  失败:", str(exc)[:120], flush=True)
        saved["n"] += 1


with DouyinBrowser(ROOT / ".probe_browser", headless=True, log=log) as br:
    res = br.collect_folders(full=False, on_items=on_items)
print(f"扫到 {len(res['folders'])} 个收藏夹，尝试保存 {saved['n']} 条")

print("\n落盘结果：")
works = []
for d1 in sorted(x for x in tmp.iterdir() if x.is_dir()):
    for d2 in sorted(x for x in d1.iterdir() if x.is_dir()):
        if d2.name[-6:].isdigit():
            works.append(d2)
            print("  ", d2.relative_to(tmp).as_posix(), "→", [f.name for f in d2.iterdir()][:4])
if works:
    depth = len(works[0].relative_to(tmp).parts)
    print(f"\n目录层级 = {depth}（2 = 收藏夹/作品 ✓）")
else:
    print("\n没落下作品")
print("清理临时目录…")
import shutil  # noqa: E402

shutil.rmtree(tmp, ignore_errors=True)
