"""配置文件读写。"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _app_root() -> Path:
    """打包成 exe 后，配置和下载目录放在 exe 旁边，而不是临时解包目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _app_root()
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    "download_dir": str(ROOT / "downloads"),
    "headless": True,
    "max_user_videos": 50,
    # 遇到已经下过的作品怎么办：skip = 跳过；overwrite = 重新下载并覆盖；ask = 每条问一次
    "duplicate": "skip",
    "video_codec": "h264",   # h264 = 兼容性最好；best = 码率最高的那档（可能是 h265）
    "baidu": {
        "enabled": True,
        "mode": "copy",          # copy = 同步文件夹；api = 开放平台直传；off = 只存本地
        "sync_dir": "",          # 百度网盘客户端的本地同步文件夹
        # ---- 开放平台（api 模式）----
        "app_name": "",          # 创建应用时填的名称，决定 /apps/{应用名} 目录
        "app_key": "",           # 应用的 AppKey
        "secret_key": "",        # 应用的 SecretKey
        "access_token": "",
        "refresh_token": "",
        "expires_at": 0,
        "redirect_uri": "http://localhost:8899/callback",
        "remote_dir": "抖音备份",   # 老字段，两个板块分开之前的目录名（兼容旧配置）
        # 两个板块各自的网盘目录（都在「我的应用数据」下）
        "remote_dir_collect": "抖音收藏",   # 收藏自动备份 → /apps/抖音收藏
        "remote_dir_link": "视频备份",      # 视频下载     → /apps/视频备份
    },
    # 自动同步手机上的「收藏」
    "auto_sync": {
        "enabled": False,
        "interval_minutes": 10,
        "scope": "both",         # both = 收藏+点赞（默认）；collect = 只要收藏；like = 只要点赞
        "like_limit": 1000,      # 比对点赞时最多取多少条
        "full_scan": False,      # True = 每次都把收藏夹翻到底（慢，默认只取最新一页）
    },
    # 收藏夹分类
    "organize": {
        "by_collect_folder": True,   # 按抖音收藏夹建子文件夹：<收藏夹>/<作品>
        "flat_in_folder": True,      # 收藏板块不再套作者层（<收藏夹>/<作品>）
        "fallback_folder": "视频",    # 收藏/点赞了但不属于任何收藏夹的作品放这里
    },
    "images": {
        "save_images": True,
        "save_bgm": True,
        "make_video": True,
        "image_seconds": 2.6,
    },
}


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    if CONFIG_PATH.exists():
        try:
            return _merge(DEFAULTS, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            return json.loads(json.dumps(DEFAULTS))
    return json.loads(json.dumps(DEFAULTS))


def save(cfg: dict) -> dict:
    merged = _merge(DEFAULTS, cfg)
    CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged
