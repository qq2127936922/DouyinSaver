"""抖音收藏转存工具 · 命令行入口

用法：
    python cli.py                          # 交互模式：粘贴分享链接/文案，回车结束
    python cli.py "https://v.douyin.com/xxx"
    python cli.py "链接1" "链接2"           # 多个链接
    python cli.py -f links.txt              # 从文件批量读链接（一行一个）
    python cli.py --show-browser "链接"     # 显示浏览器，方便手动过验证码
    python cli.py --setup                   # 首次配置（百度网盘保存目录）
    python cli.py --show                    # 查看当前配置
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config as cfgmod  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402

BANNER = """
──────────────────────────────────────────────
  抖音收藏 → 无水印 → 百度网盘
  支持：单条视频 / 图文(带背景音乐) / 博主主页全部作品
──────────────────────────────────────────────
"""


def ask(prompt: str, default: str = "") -> str:
    tip = f"{prompt}" + (f" [{default}]" if default else "") + "："
    try:
        val = input(tip).strip()
    except EOFError:
        return default
    return val or default


def setup() -> None:
    cfg = cfgmod.load()
    print("\n【1】文件先存到哪（本地下载目录）")
    cfg["download_dir"] = ask("本地目录", cfg["download_dir"])

    print("\n【2】怎么进百度网盘")
    print("  1) 同步文件夹（推荐，零配置）：文件丢进百度网盘客户端的同步目录，客户端自动上传")
    print("  2) 开放平台 API：需要自己申请 access_token，直传到网盘目录")
    print("  3) 不传网盘，只存本地")
    choice = ask("选择", "1")
    if choice == "1":
        print("  提示：百度网盘客户端 → 设置 → 同步空间，就能看到同步文件夹路径")
        cfg["baidu"]["mode"] = "copy"
        cfg["baidu"]["sync_dir"] = ask("同步文件夹路径", cfg["baidu"].get("sync_dir", ""))
    elif choice == "2":
        cfg["baidu"]["mode"] = "api"
        cfg["baidu"]["access_token"] = ask("access_token", cfg["baidu"].get("access_token", ""))
        cfg["baidu"]["remote_dir"] = ask("网盘目录", cfg["baidu"].get("remote_dir", "/抖音下载"))
    else:
        cfg["baidu"]["mode"] = "off"

    print("\n【3】博主主页最多抓多少条（0 = 不限）")
    cfg["max_user_videos"] = int(ask("数量", str(cfg.get("max_user_videos", 50))) or 50)

    cfgmod.save(cfg)
    print(f"\n已保存配置：{cfgmod.CONFIG_PATH}\n")


def interactive(p: Pipeline, headless: bool) -> None:
    print(BANNER)
    print("把分享链接或整段分享文案粘进来，一行一个；直接回车开始下载。\n")
    lines: list[str] = []
    while True:
        try:
            line = input("> " if not lines else "  ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        lines.append(line)
    if not lines:
        print("没有输入链接。")
        return
    for i, text in enumerate(lines, 1):
        print(f"\n[{i}/{len(lines)}] 处理：{text[:60]}")
        p.run_link(text, headless=headless)
    print("\n" + p.summary())


def main() -> None:
    ap = argparse.ArgumentParser(add_help=True, description="抖音无水印下载并转存百度网盘")
    ap.add_argument("links", nargs="*", help="抖音分享链接或分享文案")
    ap.add_argument("-f", "--file", help="从文本文件读取链接（一行一个）")
    ap.add_argument("--show-browser", action="store_true", help="显示浏览器窗口（遇到验证码时可手动通过）")
    ap.add_argument("--again", action="store_true", help="已下载过的作品也重新下载并覆盖")
    ap.add_argument("--setup", action="store_true", help="首次配置")
    ap.add_argument("--show", action="store_true", help="查看当前配置")
    args = ap.parse_args()

    if args.setup:
        setup()
        return
    if args.show:
        import json

        print(json.dumps(cfgmod.load(), ensure_ascii=False, indent=2))
        return

    headless = not args.show_browser
    cfg = cfgmod.load()
    if args.again:
        cfg["duplicate"] = "overwrite"
    p = Pipeline(cfg=cfg, log=print, upload_kind="link")   # 命令行都是分享链接 → 网盘「视频备份」

    links = list(args.links)
    if args.file:
        links += [
            ln.strip()
            for ln in Path(args.file).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    if links:
        for i, text in enumerate(links, 1):
            print(f"\n[{i}/{len(links)}] 处理：{text[:60]}")
            p.run_link(text, headless=headless)
        print("\n" + p.summary())
    else:
        interactive(p, headless)


if __name__ == "__main__":
    main()
