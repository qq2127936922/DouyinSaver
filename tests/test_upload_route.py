"""补传分流规则：命中收藏夹名 → 收藏板块，其余按下载板块。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import upload_kind_for  # noqa: E402

names = {"古风", "AIGC", "8分", "视频"}
cases = [
    ("古风/20240101_标题_123456", "collect"),        # 收藏夹/作品
    ("视频/20240103_标题_789012", "collect"),        # 兜底「视频」文件夹
    ("某作者/20260926_标题_564658", "link"),          # 下载板块 作者/作品
    ("古风/某作者/20240101_标题_123456", "collect"),  # 老的三层结构
    ("AIGC/20240105_标题_111111", "collect"),
]
ok = 0
for rel, want in cases:
    got = upload_kind_for(Path(rel), names)
    flag = "PASS" if got == want else "FAIL"
    ok += got == want
    print(f"  {flag} {rel} → {got}")
print(f"通过 {ok}/{len(cases)}")
sys.exit(0 if ok == len(cases) else 1)
