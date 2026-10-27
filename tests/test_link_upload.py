"""真机端到端：用一条真实作品走「视频下载」板块，确认下到本地并传到 /apps/视频备份。"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import downloader as dl  # noqa: E402
from core.config import _merge  # noqa: E402
from core.douyin import DouyinBrowser  # noqa: E402
from core.pipeline import Pipeline, pan_remote_dir  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
install_cfg = json.loads((ROOT / "抖音收藏助手/config.json").read_text(encoding="utf-8"))
map_path = ROOT / "抖音收藏助手/downloads/.collect_map.json"
aid = next(iter(json.loads(map_path.read_text(encoding="utf-8"))["map"]))
print("测试作品 id:", aid)

tmp = Path(tempfile.mkdtemp(prefix="dsy_link_"))
cfg = _merge(install_cfg, {
    "download_dir": str(tmp),
    "duplicate": "overwrite",
    "baidu": {"enabled": True, "mode": "api", "sync_dir": ""},
})
cfg["profile_dir"] = str(ROOT / ".probe_browser")

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.1f}s] {m}", flush=True)  # noqa: E731
p = Pipeline(cfg=cfg, log=log, upload_kind="link")
print("上传根目录:", p.pan.remote_root(), "（应为", "/apps/" + pan_remote_dir(cfg, "link"), "）")

with DouyinBrowser(ROOT / ".probe_browser", headless=True, log=log) as br:
    aweme = br.fetch_aweme(aid)
meta = dl.aweme_meta(aweme)
print("作品:", meta["author"], "|", meta["title"][:40], "| 类型", meta["kind"])

ok = p.save_aweme(aweme)          # folder=None → 作者/作品
print("save_aweme:", ok)
for f in sorted(tmp.rglob("*")):
    if f.is_file():
        print("  本地:", f.relative_to(tmp).as_posix(), f.stat().st_size, "字节")

print("\n网盘 /apps/视频备份 下：")
for d in p.pan.list_dir("/apps/视频备份"):
    print("  ", "DIR " if d["isdir"] else "FILE", d["name"])
    if d["isdir"] and d["name"] == dl.safe_name(meta["author"], 40):
        for w in p.pan.list_dir(d["path"]):
            print("      ", "DIR " if w["isdir"] else "FILE", w["name"])

print("\n测试目录留在这儿方便核对：", tmp)
