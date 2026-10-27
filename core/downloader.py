"""媒体解析与下载：无水印视频 / 图集 / 背景音乐，图集可合成带 BGM 的视频。"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}

INVALID = re.compile(r'[\\/:*?"<>|\r\n\t]')

# 打包后是无窗口的窗口程序，ffmpeg 属于控制台程序，
# 不显式指定 CREATE_NO_WINDOW 每次调用都会闪出一个黑窗口。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _quiet_run(cmd: list[str], **kwargs):
    if _NO_WINDOW:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def safe_name(text: str, maxlen: int = 60) -> str:
    text = INVALID.sub(" ", (text or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:maxlen].strip() or "无标题")


def media_kind(aweme: dict) -> str:
    """'images' = 图文/图集，'video' = 视频。"""
    images = aweme.get("images") or []
    if images and not (aweme.get("video") or {}).get("play_addr"):
        return "images"
    if images and aweme.get("aweme_type") in (68, 150):
        return "images"
    return "images" if images else "video"


def pick_video_urls(aweme: dict, prefer: str = "h264") -> list[str]:
    """优先取最高码率的无水印地址（play_addr 无水印，download_addr 带水印）。

    prefer="h264" 时优先 h264（兼容性好，手机/电视/网盘在线播放都不挑），
    没有 h264 才退回 h265；prefer="best" 则纯按码率挑。
    """
    video = aweme.get("video") or {}
    ranked: list[tuple[int, int, list[str]]] = []
    for br in video.get("bit_rate") or []:
        addr = (br.get("play_addr") or {}).get("url_list") or []
        if addr:
            ranked.append((0 if br.get("is_h265") else 1, int(br.get("bit_rate") or 0), addr))
    if prefer == "best":
        ranked.sort(key=lambda x: x[1], reverse=True)
    elif any(r[0] == 1 for r in ranked):
        ranked = [r for r in ranked if r[0] == 1]
        ranked.sort(key=lambda x: x[1], reverse=True)
    else:
        ranked.sort(key=lambda x: x[1], reverse=True)

    urls: list[str] = []
    for _, _, addr in ranked:
        urls.extend(u for u in addr if u not in urls)
    for key in ("play_addr", "play_addr_h264", "play_addr_265"):
        for u in (video.get(key) or {}).get("url_list") or []:
            if u not in urls:
                urls.append(u)
    return urls


def pick_images(aweme: dict) -> list[str]:
    """图集取无水印原图。

    注意：download_url_list 里带的是 `tplv-dy-water` 水印图，
    真正的无水印图在 url_list（`tplv-dy-aweme-images`）。所以一律排除含水印的地址。
    """
    out: list[str] = []
    for img in aweme.get("images") or []:
        urls = [u for u in (img.get("url_list") or []) if "water" not in u.lower()]
        if not urls:
            urls = [u for u in (img.get("download_url_list") or []) if "water" not in u.lower()]
        if not urls:
            urls = img.get("url_list") or img.get("download_url_list") or []
        jpegs = [u for u in urls if ".jpeg" in u.lower() or "mime_type=image%2Fjpeg" in u.lower()]
        out.append((jpegs or urls)[-1])
    return out


def is_live_photo(img: dict) -> bool:
    """图集里的「实况/动态图」：本体是一段小视频，合成时按自然时长播放。

    实测有两种标记：实况图 live_photo_type=1（clip_type=5，林黛玉那种）、
    动态图 clip_type=3（GIF 图，无 live_photo_type 字段）。
    两者都把小视频挂在 images[i].video 下，所以直接以「带 video.play_addr」为准，
    不看具体标记值（普通静态图没有 video 字段）。
    """
    if not isinstance(img, dict):
        return False
    v = img.get("video") or {}
    return bool((v.get("play_addr") or {}).get("url_list"))


def pick_live_clip_urls(aweme: dict) -> list[list[str]]:
    """按图集顺序返回每张的实况视频地址；普通图是空列表。

    实况视频挂在 images[i].video 下（play_addr_h264 优先，h265 老机器放不动）。
    """
    out: list[list[str]] = []
    for img in aweme.get("images") or []:
        urls: list[str] = []
        if is_live_photo(img):
            v = img.get("video") or {}
            for key in ("play_addr_h264", "play_addr"):
                addr = v.get(key) or {}
                urls += [u for u in (addr.get("url_list") or []) if "water" not in u.lower()]
            if not urls:
                for b in v.get("bit_rate") or []:
                    urls += [
                        u for u in ((b.get("play_addr") or {}).get("url_list") or [])
                        if "water" not in u.lower()
                    ]
        out.append(urls)
    return out


def img_suffix(url: str) -> str:
    low = (url or "").lower()
    if "webp" in low:
        return ".webp"
    if "png" in low:
        return ".png"
    return ".jpg"


def pick_music(aweme: dict) -> str | None:
    """背景音乐 / 原声地址。图文作品若为纯图文，可能没有音轨。"""
    music = aweme.get("music") or {}
    for key in ("play_url", "play_url_h265"):
        urls = (music.get(key) or {}).get("url_list") or []
        for u in urls:
            if u and not u.endswith(".mp3"):
                continue
            return u
    for key in ("play_url", "play_url_h265"):
        urls = (music.get(key) or {}).get("url_list") or []
        if urls:
            return urls[0]
    audio = (aweme.get("video") or {}).get("audio") or {}
    urls = (audio.get("play_url") or {}).get("url_list") or []
    return urls[0] if urls else None


def _music_suffix(url: str) -> str:
    """按地址猜后缀：抖音音乐直链多为 .mp3，video.audio / 页面流是 m4a 容器。"""
    path = url.split("?")[0].lower()
    return ".mp3" if path.endswith(".mp3") else ".m4a"


def detect_audio_suffix(path: Path) -> str:
    """按文件头判断真实音频格式。抖音不少 .mp3 地址其实返回 m4a 容器。"""
    try:
        head = Path(path).read_bytes()[:16]
    except Exception:  # noqa: BLE001
        return ".mp3"
    if b"ftyp" in head:
        return ".m4a"
    return ".mp3"


def pick_music_detail(aweme: dict) -> tuple[str, str] | None:
    """返回 (音频地址, 建议后缀)。

    版权音乐的接口直链是空的（music.play_url = []），这时用作品页播放流的地址兜底，
    由 DouyinBrowser.fetch_aweme 顺手截获，挂在 aweme["_fallback_music"]。
    """
    url = pick_music(aweme)
    if url:
        return url, _music_suffix(url)
    fb = aweme.get("_fallback_music")
    if fb and fb[0]:
        return fb[0], (fb[1] if len(fb) > 1 else ".mp3")
    return None


def aweme_meta(aweme: dict) -> dict:
    """整理成统一的作品描述。"""
    author = (aweme.get("author") or {}).get("nickname") or "未知作者"
    desc = aweme.get("desc") or ""
    ts = aweme.get("create_time") or 0
    try:
        date = datetime.fromtimestamp(ts).strftime("%Y%m%d")
    except Exception:  # noqa: BLE001
        date = "00000000"
    aweme_id = str(aweme.get("aweme_id") or "")
    kind = media_kind(aweme)
    stat = aweme.get("statistics") or {}
    return {
        "aweme_id": aweme_id,
        "author": author,
        "desc": desc,
        "date": date,
        "kind": kind,
        "url": f"https://www.douyin.com/video/{aweme_id}",
        "digg": stat.get("digg_count"),
        "comment": stat.get("comment_count"),
        "title": f"{date}_{safe_name(desc, 36)}_{aweme_id[-6:]}",
    }


def download_file(url: str, dest: Path, log=None, retries: int = 3) -> Path:
    log = log or (lambda m: None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last = None
    for i in range(1, retries + 1):
        try:
            with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                if total and done < total * 0.9:
                    raise IOError(f"下载不完整 {done}/{total}")
            tmp.replace(dest)
            return dest
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"  下载重试 {i}/{retries}：{exc}")
            resp = getattr(exc, "response", None)
            code = int(getattr(resp, "status_code", 0) or 0)
            if 400 <= code < 500 and code != 429:
                # 403/404 是签名地址过期，同一个地址再试也没用，交给上层换新地址
                break
            time.sleep(1.2 * i)
    if tmp.exists():
        tmp.unlink(missing_ok=True)
    raise IOError(f"下载失败：{last}")


_FFMPEG: list[str | None] = []


def ffmpeg_exe() -> str | None:
    if _FFMPEG:
        return _FFMPEG[0]
    exe = shutil.which("ffmpeg")
    if not exe:
        try:
            import imageio_ffmpeg  # type: ignore

            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001
            exe = None
    _FFMPEG.append(exe)
    return exe


def probe_media(path: Path | None) -> tuple[float | None, bool]:
    """返回 (时长秒数, 是否含音轨)，读不到返回 (None, False)。"""
    if not path:
        return None, False
    p = Path(path)
    if not p.exists():
        return None, False
    ff = ffmpeg_exe()
    if not ff:
        return None, False
    try:
        res = _quiet_run([ff, "-i", str(p)], capture_output=True, timeout=90)
    except Exception:  # noqa: BLE001
        return None, False
    txt = res.stderr.decode("utf-8", "ignore")
    m = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", txt)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    return dur, ("Audio:" in txt)


def probe_duration(path: Path | None) -> float | None:
    """用 ffmpeg 读出媒体时长（秒），读不到返回 None。"""
    return probe_media(path)[0]


def grab_last_frame(clip: Path, dest: Path) -> Path | None:
    """抽出一段小视频的最后一帧存成 png，给「实况播完定格」用。

    取不到最后一帧（seek 失败 / 文件损坏）时退回第一帧，再不行返回 None。
    """
    ff = ffmpeg_exe()
    if not ff or not Path(clip).exists():
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dur = probe_duration(clip)
    plans = []
    if dur and dur > 0.15:
        plans.append(["-ss", f"{max(0.0, dur - 0.12):.3f}"])
    plans.append([])                     # 兜底：从头取第一帧
    for pre in plans:
        res = _quiet_run(
            [ff, "-y"] + pre + ["-i", str(clip), "-frames:v", "1", "-q:v", "2", str(dest)],
            capture_output=True,
        )
        if res.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return dest
    dest.unlink(missing_ok=True)
    return None


def make_slideshow(
    images: list[Path],
    bgm: Path | None,
    out: Path,
    per_image: float = 2.6,
    log=None,
    clips: list[Path | None] | None = None,
) -> Path | None:
    """图集 + 背景音乐合成 mp4。

    对齐抖音的真实播放方式：以背景音乐时长为准。
    - 音乐比一轮图片长 → 图片循环轮播把音乐铺满，音乐完整放完
    - 音乐比一轮图片短 → 按音乐节奏压缩每张停留时间，图与音乐同时结束
    clips 与 images 对齐：实况图的位置放它的小视频，播放时按自然时长插入，
    不再当静态图冻成一帧。
    """
    ff = ffmpeg_exe()
    if not ff or not images:
        return None
    log = log or (lambda m: None)
    w, h = 1080, 1920
    n = len(images)
    clips = list(clips or [])
    while len(clips) < n:
        clips.append(None)

    bgm_path = Path(bgm) if bgm else None
    has_bgm = bool(bgm_path and bgm_path.exists())
    music_dur = probe_duration(bgm_path) if has_bgm else None

    per = float(per_image) if per_image else 2.6
    # 实况位用小视频的自然时长（一般一两秒）；探不出时长就按普通图处理
    use_clip: list[bool] = []
    slot_dur: list[float] = []
    for i in range(n):
        c = clips[i]
        d = probe_duration(Path(c)) if c and Path(c).exists() else None
        ok = bool(c and d and d > 0.2)
        use_clip.append(ok)
        slot_dur.append(max(0.4, min(d or 0.0, 15.0)) if ok else per)

    if music_dur and music_dur > 0.3:
        if music_dur < sum(slot_dur):
            # 音乐比一轮还短：压普通图的停留时间，实况保持原速
            n_static = sum(1 for ok in use_clip if not ok)
            clip_sum = sum(d for i, d in enumerate(slot_dur) if use_clip[i])
            free = max(0.2, (music_dur - clip_sum) / max(1, n_static))
            per = min(per, free)
            slot_dur = [d if use_clip[i] else per for i, d in enumerate(slot_dur)]
        total = music_dur
    else:
        total = sum(slot_dur)

    # 播放策略：实况动图只在第 1 轮播一次，之后定格它的最后一帧（不再重复抖动）。
    # 普通图照旧按 per 停留、随轮次循环，把音乐铺满。
    round_dur = max(0.2, sum(slot_dur))
    total = max(0.2, total)
    reps = max(1, math.ceil((total - 0.02) / round_dur))
    live_idx = [i for i in range(n) if use_clip[i]]
    n_live = len(live_idx) * reps
    if live_idx and reps > 1:
        tip = f"，{len(live_idx)} 段实况动图播 1 轮后定格最后一帧"
    else:
        tip = f"，其中实况动图 {n_live} 段" if n_live else ""
    log(f"  图片循环 {reps} 轮{tip}，音乐放完即结束（总长 {total:.1f}s）")

    # 定格用图：每段实况取最后一帧
    still: list[Path | None] = [None] * n
    for i in live_idx:
        still[i] = grab_last_frame(Path(clips[i]), out.with_name(f"{out.stem}.__last{i}.png"))

    def round_inputs(anim: bool) -> tuple[list[str], str]:
        """一轮的输入参数 + 滤镜图。anim=True 时实况放小视频，否则用定格帧。"""
        args: list[str] = []
        for i in range(n):
            if anim and use_clip[i]:
                args += ["-i", str(clips[i])]                 # 实况：按自然时长播完
            else:
                src = still[i] if use_clip[i] else images[i]
                if src is None:
                    # 抽帧失败：退回原图（对实况位来说就是那段小视频的第一帧）
                    src = images[i]
                args += ["-loop", "1", "-t", f"{slot_dur[i]:.3f}", "-i", str(src)]
        parts = []
        for k in range(n):
            parts.append(
                f"[{k}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,"
                f"setpts=PTS-STARTPTS[v{k}]"
            )
        parts.append("".join(f"[v{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[v]")
        return args, ";".join(parts)

    enc = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart"]
    tmps: list[Path] = []

    def render_round(anim: bool) -> Path | None:
        """把一轮渲成临时片（无音轨）。命令行长度只跟图片张数有关，跟轮数无关。"""
        p = out.with_name(f"{out.stem}.__r{1 if anim else 2}.mp4")
        args, graph = round_inputs(anim)
        cmd = [ff, "-y"] + args + [
            "-filter_complex", graph, "-map", "[v]", "-t", f"{round_dur:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
            "-pix_fmt", "yuv420p", "-an", str(p),
        ]
        res = _quiet_run(cmd, capture_output=True)
        if res.returncode != 0 or not p.exists():
            err = res.stderr.decode("utf-8", "ignore").strip().splitlines()
            log(f"  图集合成失败（预处理一轮）：{(err[-1] if err else '未知错误')[:180]}")
            p.unlink(missing_ok=True)
            return None
        tmps.append(p)
        return p

    if reps <= 1:
        args, graph = round_inputs(True)
        cmd = [ff, "-y"] + args
        if has_bgm:
            cmd += ["-i", str(bgm_path)]
        cmd += ["-filter_complex", graph, "-map", "[v]"]
        if has_bgm:
            # apad 只是兜底补静音，配合 -t 保证音轨铺满，不会提前结束
            cmd += ["-map", f"{n}:a", "-af", "apad", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-t", f"{total:.3f}", "-crf", "23"] + enc + [str(out)]
    elif not live_idx:
        # 全是静态图：一轮就是整轮画面，直接循环它，不会出现命令行过长
        p = render_round(True)
        if not p:
            return None
        cmd = [ff, "-y", "-stream_loop", str(reps - 1), "-i", str(p)]
        if has_bgm:
            cmd += ["-i", str(bgm_path), "-map", "0:v", "-map", "1:a", "-af", "apad",
                    "-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-map", "0:v"]
        cmd += ["-t", f"{total:.3f}", "-crf", "23"] + enc + [str(out)]
    else:
        # 有实况：首轮播动图，第 2 轮起定格，两种轮次各渲一次再拼
        first = render_round(True)
        rest = render_round(False) if first else None
        if not (first and rest):
            for t in tmps:
                t.unlink(missing_ok=True)
            return None
        cmd = [ff, "-y", "-i", str(first), "-stream_loop", str(max(0, reps - 2)),
               "-i", str(rest)]
        if has_bgm:
            cmd += ["-i", str(bgm_path)]
        cmd += ["-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]"]
        if has_bgm:
            cmd += ["-map", "2:a", "-af", "apad", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-t", f"{total:.3f}", "-crf", "23"] + enc + [str(out)]

    try:
        res = _quiet_run(cmd, capture_output=True)
    finally:
        for t in tmps:
            t.unlink(missing_ok=True)
        for i in live_idx:
            if still[i] is not None:
                still[i].unlink(missing_ok=True)
    if res.returncode != 0 or not out.exists():
        err = res.stderr.decode("utf-8", "ignore").strip().splitlines()
        log(f"  图集合成失败：{(err[-1] if err else '未知错误')[:180]}")
        return None
    return out
