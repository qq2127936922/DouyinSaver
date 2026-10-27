"""真机端到端：收藏板块，确认传到 /apps/抖音收藏/<收藏夹>/<作者>/<作品>，然后清理测试数据。"""
import json
import sys
import tempfile
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import baidu as baimod  # noqa: E402
from core import downloader as dl  # noqa: E402
from core.config import _merge  # noqa: E402
from core.douyin import DouyinBrowser  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
install_cfg = json.loads((ROOT / "抖音收藏助手/config.json").read_text(encoding="utf-8"))
aid = next(iter(json.loads((ROOT / "抖音收藏助手/downloads/.collect_map.json").read_text(encoding="utf-8"))["map"]))

tmp = Path(tempfile.mkdtemp(prefix="dsy_col_"))
cfg = _merge(install_cfg, {"download_dir": str(tmp), "duplicate": "overwrite",
                           "baidu": {"enabled": True, "mode": "api", "sync_dir": ""}})
cfg["profile_dir"] = str(ROOT / ".probe_browser")

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.1f}s] {m}", flush=True)  # noqa: E731
p = Pipeline(cfg=cfg, log=log, upload_kind="collect")
print("上传根目录:", p.pan.remote_root())

with DouyinBrowser(ROOT / ".probe_browser", headless=True, log=log) as br:
    aweme = br.fetch_aweme(aid)
meta = dl.aweme_meta(aweme)
p.save_aweme(aweme, folder="测试收藏夹")
print("本地:", [f.relative_to(tmp).as_posix() for f in tmp.rglob('*') if f.is_file()][:3])

print("\n/apps/抖音收藏 下：")
for d in p.pan.list_dir("/apps/抖音收藏"):
    print("  ", "DIR " if d["isdir"] else "FILE", d["name"])
    if d["isdir"] and d["name"] == "测试收藏夹":
        for a in p.pan.list_dir(d["path"]):
            print("      ", "DIR ", a["name"])
            for w in p.pan.list_dir(a["path"]):
                print("          ", "DIR ", w["name"][:50])


def rmtree(path):
    r = requests.post(baimod.XPAN_FILE,
                      params={"method": "filemanager", "opera": "delete",
                              "access_token": p.pan.token, "async": 0},
                      data={"filelist": json.dumps([path])},
                      headers={"User-Agent": "pan.baidu.com"}, timeout=60).json()
    print("清理", path, "errno =", r.get("errno"))


print("\n清理测试数据：")
rmtree("/apps/抖音收藏/测试收藏夹")
rmtree("/apps/视频备份/文武双全的Agent")
print("/apps/抖音收藏 现在:", [d["name"] for d in p.pan.list_dir("/apps/抖音收藏")])
print("/apps/视频备份 现在:", [d["name"] for d in p.pan.list_dir("/apps/视频备份")])
