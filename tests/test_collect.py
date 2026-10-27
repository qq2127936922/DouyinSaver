"""真机自测：读收藏夹（首屏 / 少量全量），不下载。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.douyin import DouyinBrowser  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
mode = sys.argv[1] if len(sys.argv) > 1 else "recent"
t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.1f}s] {m}", flush=True)  # noqa: E731

with DouyinBrowser(ROOT / ".probe_browser", headless=True, log=log) as br:
    if mode == "recent":
        res = br.collect_folders(full=False)
        print("folders:", len(res["folders"]))
        print("id2folder:", len(res["id2folder"]))
        print("got:", json.dumps(res["got"], ensure_ascii=False)[:600])
        print("前 8 个:", json.dumps(res["folders"][:8], ensure_ascii=False))
    else:
        def on_items(folder, awemes):
            print(f"  >> {folder['name']}: {len(awemes)} 条，例如 "
                  f"{(awemes[0].get('desc') or '')[:20]}", flush=True)

        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        res = br.collect_folders(full=True, limit_folders=limit, on_items=on_items)
        print("folders:", len(res["folders"]))
        print("id2folder:", len(res["id2folder"]))
        (ROOT / "tc_result.json").write_text(
            __import__("json").dumps({"got": res["got"], "declared":
                {f["name"]: f["count"] for f in res["folders"]}}, ensure_ascii=False, indent=1),
            encoding="utf-8")
print(f"总耗时 {time.time()-t0:.0f}s")
