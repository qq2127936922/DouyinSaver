"""抖音链接解析：从分享文案 / 短链 / 各种 URL 中提取作品或作者标识。"""
from __future__ import annotations

import re

# 分享文案里常见的中文标点、表情，URL 到这些字符为止
URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff，,。；;、！!？?\"'“”‘’<>（）()【】\[\]]+")

VIDEO_PATTERNS = [
    re.compile(r"douyin\.com/video/(\d{15,25})"),
    re.compile(r"douyin\.com/note/(\d{15,25})"),
    re.compile(r"iesdouyin\.com/share/(?:video|note|slides)/(\d{15,25})"),
    re.compile(r"[?&]modal_id=(\d{15,25})"),
    re.compile(r"[?&]aweme_id=(\d{15,25})"),
]

USER_PATTERNS = [
    re.compile(r"douyin\.com/user/([A-Za-z0-9_\-]{20,})"),
    re.compile(r"iesdouyin\.com/share/user/([A-Za-z0-9_\-]{20,})"),
]


def extract_url(text: str) -> str | None:
    """从任意文本中抠出第一个 http(s) 链接。"""
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip(".,;:") if m else None


def classify(url: str) -> dict:
    """判断链接类型。返回 {'kind': 'video'|'user'|'short', ...}"""
    url = url.strip()
    for pat in VIDEO_PATTERNS:
        m = pat.search(url)
        if m:
            return {"kind": "video", "aweme_id": m.group(1)}
    for pat in USER_PATTERNS:
        m = pat.search(url)
        if m:
            return {"kind": "user", "sec_uid": m.group(1)}
    if "v.douyin.com" in url:
        return {"kind": "short"}
    return {"kind": "unknown"}
