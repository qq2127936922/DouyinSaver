"""真机验证：图集里的实况/动态图按自然时长插进合成视频（用用户给的链接）。"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import downloader as dl  # noqa: E402
from core.config import DEFAULTS, _merge  # noqa: E402
from core.douyin import DouyinBrowser  # noqa: E402
from core.linkparse import classify, extract_url  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEXT = sys.argv[1] if len(sys.argv) > 1 else "https://v.douyin.com/0Abgo47OxjY/"
tmp = Path(tempfile.mkdtemp(prefix="dsy_live_"))
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

with DouyinBrowser(ROOT / ".probe_browser", headless=True, log=log) as br:
    real = br.resolve_short(extract_url(TEXT))
    info = classify(real)
    aweme = br.fetch_aweme(info["aweme_id"])
    ok = p.save_aweme(aweme)

print("\n落盘内容：")
files = sorted(f for f in tmp.rglob("*") if f.is_file())
for f in files:
    print(f"   {f.relative_to(tmp).as_posix()}  {f.stat().st_size // 1024} KB")

clips = [f for f in files if "_动态" in f.name]
video = next((f for f in files if f.suffix == ".mp4" and "_动态" not in f.name), None)
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        fails += 1


check("保存成功", ok)
check("下到了实况视频", bool(clips), str(files))
if clips:
    cdur = dl.probe_duration(clips[0])
    print(f"  实况时长: {cdur:.2f}s")
    check("实况是真视频（>0.2s，不是单帧）", bool(cdur and cdur > 0.2), str(cdur))
    check("合成视频出来了", bool(video and video.exists()))
    if video:
        vdur = dl.probe_duration(video)
        got, has_audio = dl.probe_media(video)
        print(f"  成片时长: {vdur:.1f}s  有音轨: {has_audio}")
        check("成片有音轨", has_audio)
        # 抽成片里实况段的两帧，确认画面在动（实况从第 1 段图之后开始）
        ff = dl.ffmpeg_exe()
        t1 = 2.8
        t2 = t1 + max(0.4, (cdur or 1.0) - 0.4)
        fr = []
        for k, tt in enumerate((t1, t2)):
            f = tmp / f"frame{k}.png"
            subprocess.run([ff, "-y", "-ss", f"{tt:.2f}", "-i", str(video),
                            "-frames:v", "1", str(f)], capture_output=True, timeout=120)
            fr.append(f)
        if all(f.exists() for f in fr):
            b1, b2 = fr[0].read_bytes(), fr[1].read_bytes()
            check("实况段画面在动（两帧不同）", b1 != b2, f"{len(b1)} vs {len(b2)}")
        else:
            check("实况段画面在动（两帧不同）", False, "抽帧失败")

shutil.rmtree(tmp, ignore_errors=True)
print("\n失败数:", fails)
sys.exit(1 if fails else 0)
