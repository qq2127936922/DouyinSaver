"""修复旧版本合成的图文视频。

旧版本按「图片数 × 2.6 秒」出片，图放完就把背景音乐掐断。
这个脚本扫描下载目录，把「视频比背景音乐短」的图文重新合成一遍，
直接用本地已有的图片和音乐，不需要重新下载。

用法：
    python tools/repair.py            # 修默认下载目录
    python tools/repair.py D:\\抖音下载
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config as cfgmod  # noqa: E402
from core import downloader as dl  # noqa: E402

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def collect(root: Path, per_image: float) -> list[tuple[Path, list[Path], Path]]:
    tasks = []
    found = sorted(root.rglob("背景音乐.mp3")) + sorted(root.rglob("背景音乐.m4a"))
    for bgm in found:
        img_dir = bgm.parent
        imgs = sorted(p for p in img_dir.iterdir() if p.suffix.lower() in IMG_EXT)
        if not imgs:
            continue
        music_len = dl.probe_duration(bgm)
        if not music_len:
            continue
        # 新布局：mp4 和图片在同一个作品文件夹里；旧布局：mp4 在上层作者目录
        cands = [img_dir / f"{img_dir.name}.mp4", img_dir.parent / f"{img_dir.name}.mp4"]
        mp4 = next((p for p in cands if p.exists()), cands[0])
        got = dl.probe_duration(mp4) if mp4.exists() else 0
        if got and got >= music_len * 0.9:
            continue
        tasks.append((mp4, imgs, bgm))
    return tasks


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(cfgmod.load()["download_dir"])
    if not root.exists():
        print(f"目录不存在：{root}")
        return 1
    per_image = float((cfgmod.load().get("images") or {}).get("image_seconds", 2.6) or 2.6)
    tasks = collect(root, per_image)
    if not tasks:
        print("没有需要修复的图文视频。")
        return 0
    print(f"找到 {len(tasks)} 个需要修复的图文，开始重做（每条几十秒）…\n")
    ok = fail = 0
    for i, (mp4, imgs, bgm) in enumerate(tasks, 1):
        name = mp4.name[:48]
        print(f"[{i}/{len(tasks)}] {name}")
        tmp = mp4.with_suffix(".repair.mp4")
        try:
            if dl.make_slideshow(imgs, bgm, tmp, per_image=per_image, log=print):
                os.replace(tmp, mp4)
                print(f"  ✓ 已修复 {dl.probe_duration(mp4):.1f}s")
                ok += 1
            else:
                print("  × 合成失败，已保留原文件")
                fail += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  × 出错：{exc}")
            fail += 1
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
    print(f"\n完成：修复 {ok} 个，失败 {fail} 个")
    return 0 if not fail else 2


if __name__ == "__main__":
    sys.exit(main())
