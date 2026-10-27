"""离线验证：签名地址 403 过期后，重抓一次详情、用新地址下成功。

不联网：把 download_file 换掉，旧地址一律抛 403，新地址写一个假 mp4。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import downloader as dl  # noqa: E402
from core.config import DEFAULTS, _merge  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402


def make_aweme(aid: str, urls: list[str]) -> dict:
    return {
        "aweme_id": aid,
        "desc": "测试地址过期",
        "create_time": 1750000000,
        "author": {"nickname": "测试作者"},
        "images": [],
        "statistics": {"digg_count": 1, "comment_count": 0},
        "video": {
            "bit_rate": [
                {"is_h265": False, "bit_rate": 1000, "play_addr": {"url_list": urls}}
            ],
            "play_addr": {"url_list": urls},
        },
    }


calls: list[str] = []


def fake_download(url: str, dest: Path, log=None, retries: int = 3) -> Path:
    calls.append(url)
    if "old" in url:
        resp = requests.Response()
        resp.status_code = 403
        err = requests.HTTPError(f"403 Client Error: Forbidden for url: {url}")
        err.response = resp
        raise err
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"\x00" * 2048)
    return dest


dl.download_file = fake_download
# save_aweme 里用的是 dl.download_file 这个模块属性，patch 生效
tmp = Path(tempfile.mkdtemp(prefix="dsy_refresh_"))
cfg = _merge(json.loads(json.dumps(DEFAULTS)), {
    "download_dir": str(tmp),
    "duplicate": "skip",
    "baidu": {"enabled": False, "mode": "off"},
    "auto_sync": {"enabled": False},
    "images": {"save_images": False, "save_bgm": False, "make_video": True},
})

logs: list[str] = []
p = Pipeline(cfg=cfg, log=lambda m: (logs.append(m), print(m))[1])

# --- 用例 1：旧地址 403，refresh 给出新地址 → 应该成功
n_refresh = {"n": 0}


def refresh_ok(aid: str) -> dict:
    n_refresh["n"] += 1
    return make_aweme(aid, ["https://new/ok.mp4"])


ok = p.save_aweme(make_aweme("7400000000000000001", ["https://old/a.mp4", "https://old/b.mp4"]),
                  folder="古风", refresh=refresh_ok)
print("\n用例1 save_aweme →", ok, "| refresh 调用", n_refresh["n"], "次")
assert ok, "应该在换新地址后下载成功"
assert n_refresh["n"] == 1, "只该重抓一次详情"
assert not any("old" in u for u in calls if u.endswith("ok.mp4"))

# --- 用例 2：refresh 拿不到数据 → 应该失败但不崩
n_refresh2 = {"n": 0}


def refresh_none(aid: str) -> dict:
    n_refresh2["n"] += 1
    return {}


ok2 = p.save_aweme(make_aweme("7400000000000000002", ["https://old/c.mp4"]),
                   folder="古风", refresh=refresh_none)
print("\n用例2 save_aweme →", ok2, "| refresh 调用", n_refresh2["n"], "次")
assert ok2 is False
assert n_refresh2["n"] == 1

# --- 用例 3：没有 refresh（老调用方式）→ 失败但行为不变
ok3 = p.save_aweme(make_aweme("7400000000000000003", ["https://old/d.mp4"]), folder="古风")
print("\n用例3 save_aweme →", ok3)
assert ok3 is False

print("\n全部通过")
shutil.rmtree(tmp, ignore_errors=True)
