"""「收藏 + 点赞」必须是交集：只收藏没点赞的不备份。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.pipeline import digged_only  # noqa: E402

A = [
    {"aweme_id": "1", "user_digged": 1},   # 收藏 + 点赞
    {"aweme_id": "2", "user_digged": 0},   # 只收藏
    {"aweme_id": "3"},                     # 字段缺失（当没点赞）
    {"aweme_id": "4", "user_digged": True},
    {"aweme_id": "5", "user_digged": None},
]
keep = [a["aweme_id"] for a in digged_only(A)]
print("保留:", keep)
ok = keep == ["1", "4"]
print("PASS" if ok else "FAIL", "交集筛选")
sys.exit(0 if ok else 1)
