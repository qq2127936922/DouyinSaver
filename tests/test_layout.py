"""离线自测：收藏板块「收藏夹/作品」平铺目录逻辑（不联网、不下载）。

规则：
  收藏板块  下载目录/<收藏夹>/<作品>/        没收藏夹 → 下载目录/视频/<作品>/
  下载板块  下载目录/<作者>/<作品>/          （分享链接、博主主页）
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import downloader as dl
from core.pipeline import Pipeline, is_work_dir

fail = 0


def check(name, cond, extra=""):
    global fail
    print(("  PASS " if cond else "  FAIL ") + name + (f"  {extra}" if (extra and not cond) else ""))
    if not cond:
        fail += 1


tmp = Path(tempfile.mkdtemp(prefix="dytest_"))
root = tmp / "downloads"
root.mkdir()

AID = "7198468849547627816"
AID2 = "7123456789012345678"
AID3 = "7000000000000000003"


def fake_aweme(aid, author="叙安", desc="山不让尘 川不辞盈"):
    return {
        "aweme_id": aid,
        "desc": desc,
        "create_time": 1691625600,
        "author": {"nickname": author},
        "video": {"play_addr": {"url_list": ["http://x/v.mp4"]}},
        "music": {"play_url": {"url_list": ["http://x/m.mp3"]}, "duration": 60000},
        "statistics": {"digg_count": 1, "comment_count": 1},
        "aweme_type": 0,
    }


def base_cfg():
    return {
        "download_dir": str(root),
        "duplicate": "skip",
        "baidu": {"enabled": False, "mode": "off"},
        "images": {"save_images": True, "save_bgm": True, "make_video": True, "image_seconds": 2.6},
        "organize": {"by_collect_folder": True, "flat_in_folder": True, "fallback_folder": "视频"},
    }


cfg = base_cfg()
a = fake_aweme(AID)
meta = dl.aweme_meta(a)

print("== 路径规则（收藏板块） ==")
p = Pipeline(cfg=cfg, log=lambda m: None)
probe = root / meta["title"]
probe.mkdir(parents=True, exist_ok=True)
check("title 以 6 位 ID 结尾", is_work_dir(probe), meta["title"])
probe.rmdir()
check(
    "有收藏夹 → 收藏夹/作品（没有作者层）",
    p._work_dir(meta, "古风") == root / "古风" / meta["title"],
    str(p._work_dir(meta, "古风")),
)
check("网盘子目录同理", p._subdir(meta, "古风") == f"古风/{meta['title']}", p._subdir(meta, "古风"))
check("没收藏夹 → 视频/作品", p._work_dir(meta) == root / "视频" / meta["title"], str(p._work_dir(meta)))
check("网盘没收藏夹 → 视频/作品", p._subdir(meta) == f"视频/{meta['title']}", p._subdir(meta))

print("== 下载板块仍然是 作者/作品 ==")
pl = Pipeline(cfg=cfg, log=lambda m: None, upload_kind="link")
check("下载板块路径", pl._work_dir(meta) == root / "叙安" / meta["title"], str(pl._work_dir(meta)))
check("下载板块网盘路径", pl._subdir(meta) == f"叙安/{meta['title']}", pl._subdir(meta))

print("== 旧结构自动归位（作者/作品 → 收藏夹/作品） ==")
old_dir = root / "叙安" / meta["title"]
old_dir.mkdir(parents=True)
(old_dir / f"{meta['title']}.mp4").write_bytes(b"x" * 1024)

p2 = Pipeline(cfg=cfg, log=lambda m: None)
check("索引能扫到旧作品", meta["title"] in p2._index)
check("_locate 能找到旧位置", p2._locate(meta, "古风") == old_dir, str(p2._locate(meta, "古风")))
p2.done.add(AID)
ok = p2.save_aweme(a, folder="古风")
new_dir = root / "古风" / meta["title"]
check("跳过时挪进收藏夹", ok and new_dir.is_dir(), str(new_dir))
check("新位置有视频", (new_dir / f"{meta['title']}.mp4").exists())
check("旧空目录被清掉", not old_dir.exists(), str(old_dir))
check("作者目录也没了", not (root / "叙安").exists())

print("== 已在新位置就不动 ==")
before = new_dir.stat().st_ino
p3 = Pipeline(cfg=cfg, log=lambda m: None)
p3.done.add(AID)
p3.save_aweme(a, folder="古风")
check("同位置不动", new_dir.is_dir() and new_dir.stat().st_ino == before)

print("== 下载板块不会把收藏夹里的作品挪走 ==")
pl.save_aweme(a)                       # 走下载板块下同一条
check("收藏夹里的作品没被搬走", new_dir.is_dir(), str(new_dir))
check("没有生成 叙安/作品", not (root / "叙安" / meta["title"]).exists())

print("== reclassify：三层拍平 + 未分层的归位 ==")
b = fake_aweme(AID2, author="初拥", desc="无标题")
mb = dl.aweme_meta(b)
(root / "初拥" / mb["title"]).mkdir(parents=True)
(root / "初拥" / mb["title"] / f"{mb['title']}.mp4").write_bytes(b"y" * 512)

# 老的「收藏夹/作者/作品」
c = fake_aweme(AID3, author="旧作者", desc="老结构")
mc = dl.aweme_meta(c)
old3 = root / "8分" / "旧作者" / mc["title"]
old3.mkdir(parents=True)
(old3 / f"{mc['title']}.mp4").write_bytes(b"z" * 256)

flags = {w: f for w, f in p3._iter_work_dirs()}
check("三层被识别为 deep", flags.get(old3) is True, str(flags.get(old3)))
check("两层被识别为非 deep", flags.get(root / "初拥" / mb["title"]) is False)

preset = {mb["title"]: "二次元", meta["title"]: "古风", mc["title"]: "8分"}
tail2folder = {k.rsplit("_", 1)[-1]: v for k, v in preset.items()}
loc = p3._reorganize_local(tail2folder)
check("搬了 2 个（未分层的 + 三层的）", loc["moved"] == 2, str(loc))
check("二次元/作品 存在（无作者层）", (root / "二次元" / mb["title"]).is_dir(), str(root / "二次元"))
check("8分/作品 拍平成功", (root / "8分" / mc["title"]).is_dir(), str(root / "8分"))
check("8分 下没残留作者目录", not (root / "8分" / "旧作者").exists())
check("古风位置没变", new_dir.is_dir())

print("== by_collect_folder 关闭 ==")
cfg2 = base_cfg()
cfg2["organize"]["by_collect_folder"] = False
cfg2["duplicate"] = "overwrite"
p4 = Pipeline(cfg=cfg2, log=lambda m: None)
check("关掉后按 作者/作品", p4._work_dir(meta, "古风") == root / "叙安" / meta["title"],
      str(p4._work_dir(meta, "古风")))

print()
print("失败数:", fail)
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fail else 0)
