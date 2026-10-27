"""两个板块的路径分流单测：确认上传目标、子目录层级都对。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import DEFAULTS, _merge  # noqa: E402
from core.pipeline import Pipeline, pan_remote_dir  # noqa: E402

ok = 0
bad = []


def check(cond, label):
    global ok
    if cond:
        ok += 1
        print("  PASS", label)
    else:
        bad.append(label)
        print("  FAIL", label)


tmp = Path(tempfile.mkdtemp(prefix="dsy_two_"))
base = _merge(json.loads(json.dumps(DEFAULTS)), {
    "download_dir": str(tmp),
    "baidu": {"enabled": False, "mode": "off"},   # 只测路径，不真上传
})

print("== 网盘目录（我的应用数据 / 这个名字） ==")
check(pan_remote_dir(base, "collect") == "抖音收藏", "收藏板块 → 抖音收藏")
check(pan_remote_dir(base, "link") == "视频备份", "下载板块 → 视频备份")

legacy = json.loads(json.dumps(base))
legacy["baidu"].pop("remote_dir_collect", None)
legacy["baidu"]["remote_dir"] = "抖音备份"
check(pan_remote_dir(legacy, "collect") == "抖音收藏", "老配置默认值 → 新目录抖音收藏")

custom = json.loads(json.dumps(legacy))
custom["baidu"]["remote_dir"] = "我的备份"
check(pan_remote_dir(custom, "collect") == "我的备份", "老配置自定义过 → 沿用用户的")

print("\n== Pipeline 按板块选根目录 ==")
p_link = Pipeline(cfg=json.loads(json.dumps(base)), log=lambda m: None, upload_kind="link")
p_collect = Pipeline(cfg=json.loads(json.dumps(base)), log=lambda m: None, upload_kind="collect")
check(p_link.pan.remote_root() == "/apps/视频备份", "link → /apps/视频备份")
check(p_collect.pan.remote_root() == "/apps/抖音收藏", "collect → /apps/抖音收藏")

with_app = json.loads(json.dumps(base))
with_app["baidu"]["app_name"] = "抖音收藏助手"
p2 = Pipeline(cfg=with_app, log=lambda m: None, upload_kind="link")
check(p2.pan.remote_root() == "/apps/抖音收藏助手/视频备份", "填了应用名时多一层")

print("\n== 上传子目录层级 ==")
meta = {"title": "20240101_标题_123456", "author": "某作者"}
check(p_collect._subdir(meta, "古风") == "古风/20240101_标题_123456",
      "收藏：收藏夹/作品（无作者层）")
check(p_collect._subdir(meta, None) == "视频/20240101_标题_123456",
      "收藏无收藏夹：视频/作品")
check(p_link._subdir(meta, None) == "某作者/20240101_标题_123456", "下载：作者/作品")

print("\n== 本地目录层级 ==")
check(p_collect._work_dir(meta, "古风").relative_to(tmp).as_posix()
      == "古风/20240101_标题_123456", "收藏本地：收藏夹/作品")
check(p_collect._work_dir(meta, None).relative_to(tmp).as_posix()
      == "视频/20240101_标题_123456", "收藏本地无收藏夹：视频/作品")
check(p_link._work_dir(meta, None).relative_to(tmp).as_posix()
      == "某作者/20240101_标题_123456", "下载本地：作者/作品")

import shutil  # noqa: E402

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n通过 {ok} 项，失败 {len(bad)} 项")
if bad:
    print("失败项:", bad)
    sys.exit(1)
