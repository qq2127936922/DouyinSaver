"""抖音收藏助手 · 桌面版

两个板块：
  ① 收藏自动备份 —— 收藏 + 点赞，严格按抖音收藏夹分类，传到网盘「抖音收藏」
  ② 视频下载     —— 分享链接 / 博主主页，传到网盘「视频备份」
"""
from __future__ import annotations

import html
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import baidu as baimod  # noqa: E402
from core import config as cfgmod  # noqa: E402
from core.douyin import has_login, qr_login  # noqa: E402
from core.douyin import profile_dir as douyin_profile  # noqa: E402
from core.pipeline import (  # noqa: E402
    Pipeline,
    collect_folder_names,
    is_work_dir,
    pan_remote_dir,
)

APP_NAME = "抖音收藏助手"


def upload_kind_for(rel: Path, folder_names: set[str] | None = None) -> str:
    """补传时判断某个作品属于哪个板块。

    收藏板块：<收藏夹>/<作品>（收藏夹名来自抖音，含兜底的「视频」）
    下载板块：<作者>/<作品>
    两者层级一样，只能靠顶层目录名认：命中收藏夹名 → 收藏板块，否则算下载板块。
    老结构「收藏夹/作者/作品」是三层，直接算收藏板块。
    """
    if len(rel.parts) >= 3:
        return "collect"
    top = rel.parts[0] if rel.parts else ""
    return "collect" if (folder_names and top in folder_names) else "link"


def iter_works(root: Path):
    """遍历作品文件夹，兼容「作者/作品」和「收藏夹/作者/作品」两种层级。"""
    try:
        top = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))
    except OSError:
        return
    for d1 in top:
        if is_work_dir(d1):
            continue
        try:
            subs = sorted(d for d in d1.iterdir() if d.is_dir())
        except OSError:
            continue
        for d2 in subs:
            if is_work_dir(d2):
                yield d2
                continue
            try:
                inner = sorted(d for d in d2.iterdir() if d.is_dir())
            except OSError:
                continue
            for d3 in inner:
                if is_work_dir(d3):
                    yield d3


# ---------------------------------------------------------------- 图标
def make_pixmap(size: int = 64) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0, QColor("#FE2C55"))
    g.setColorAt(1, QColor("#FF7A45"))
    p.setBrush(g)
    p.setPen(Qt.NoPen)
    r = size * 0.24
    p.drawRoundedRect(0, 0, size, size, r, r)

    p.setPen(QPen(QColor("#FFFFFF"), max(2.0, size * 0.075), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    cx = size * 0.5
    p.drawLine(int(cx), int(size * 0.26), int(cx), int(size * 0.56))
    p.drawLine(int(cx), int(size * 0.56), int(cx - size * 0.13), int(size * 0.43))
    p.drawLine(int(cx), int(size * 0.56), int(cx + size * 0.13), int(size * 0.43))
    p.drawLine(int(size * 0.28), int(size * 0.70), int(size * 0.72), int(size * 0.70))
    p.end()
    return pm


def ensure_icon_file() -> Path | None:
    try:
        assets = cfgmod.ROOT / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        ico = assets / "icon.ico"
        if not ico.exists():
            sizes = [16, 24, 32, 48, 64, 128, 256]
            pixmaps = [make_pixmap(s) for s in sizes]
            # Qt 的 ICO 写入只吃单张，取 256 再缩放由系统处理
            pixmaps[-1].save(str(ico), "ICO")
        return ico if ico.exists() else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 工作线程
class Worker(QThread):
    log = Signal(str)
    progress = Signal(int, int, str)
    ask_dup = Signal(str, str, object, object)   # title, author, holder, event
    finished_summary = Signal(str)

    def __init__(self, cfg: dict, links: list[str], headless: bool):
        super().__init__()
        self.cfg = cfg
        self.links = links
        self.headless = headless
        self._stop = False
        self._dup_all = ""      # "overwrite" / "skip"，选了「后面全部…」后不再逐条问

    def stop(self) -> None:
        self._stop = True

    def _ask_duplicate(self, meta: dict) -> str:
        """跑在工作线程里：把问题丢回界面线程，等用户点按钮。"""
        if self._dup_all:
            return self._dup_all
        holder = {"mode": "skip"}
        ev = threading.Event()
        self.ask_dup.emit(str(meta.get("title", "")), str(meta.get("author", "")), holder, ev)
        if not ev.wait(300):        # 界面卡住/已退出时按跳过处理
            return "skip"
        mode = str(holder.get("mode") or "skip")
        if mode == "all_overwrite":
            self._dup_all = "overwrite"
            return "overwrite"
        if mode == "all_skip":
            self._dup_all = "skip"
            return "skip"
        return mode if mode in ("overwrite", "skip") else "skip"

    def run(self) -> None:  # noqa: C901
        try:
            p = Pipeline(
                cfg=self.cfg,
                log=self.log.emit,
                on_progress=lambda d, t, label: self.progress.emit(d, t, label),
                stop=lambda: self._stop,
                ask_duplicate=self._ask_duplicate,
                upload_kind="link",          # 视频下载板块 → 网盘「视频备份」
            )
            total = len(self.links)
            for i, text in enumerate(self.links, 1):
                if self._stop:
                    self.log.emit("已手动停止")
                    break
                if total > 1:
                    self.log.emit(f"—— 第 {i}/{total} 个链接 ——")
                p.run_link(text, headless=self.headless)
            self.finished_summary.emit(p.summary())
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"运行出错：{exc}")
            self.finished_summary.emit("已中断")


# ---------------------------------------------------------------- 收藏自动同步线程
class SyncWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int, str)
    done = Signal(dict)

    def __init__(
        self, cfg: dict, headless: bool = True, scope: str = "collect",
        full: bool = False, reconcile: bool = False,
    ):
        super().__init__()
        self.cfg = cfg
        self.headless = headless
        self.scope = scope
        self.full = full
        self.reconcile = reconcile
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            auto = self.cfg.get("auto_sync") or {}
            p = Pipeline(
                cfg=self.cfg,
                log=self.log.emit,
                on_progress=lambda d, t, label: self.progress.emit(d, t, label),
                stop=lambda: self._stop,
                upload_kind="collect",       # 收藏板块 → 网盘「抖音收藏」
            )
            got = p.sync_favorites(
                headless=self.headless,
                scope=self.scope,
                full=bool(self.full or auto.get("full_scan")),
                like_limit=int(auto.get("like_limit", 1000) or 0),
                reconcile=self.reconcile,
            )
            self.done.emit(got)
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"同步出错：{exc}")
            self.done.emit({"new": 0, "total": 0, "skipped": 0, "error": str(exc)})


class ReclassifyWorker(QThread):
    """把已经下好的旧备份，按抖音收藏夹重新分文件夹（只移动，不重下）。"""

    log = Signal(str)
    progress = Signal(int, int, str)
    done = Signal(dict)

    def __init__(self, cfg: dict, headless: bool = True):
        super().__init__()
        self.cfg = cfg
        self.headless = headless
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            p = Pipeline(
                cfg=self.cfg,
                log=self.log.emit,
                on_progress=lambda d, t, label: self.progress.emit(d, t, label),
                stop=lambda: self._stop,
            )
            self.done.emit(p.reclassify(headless=self.headless))
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"整理出错：{exc}")
            self.done.emit({"moved": 0, "unknown": 0, "error": str(exc)})


# ---------------------------------------------------------------- 网盘授权线程
class PanLoginWorker(QThread):
    log = Signal(str)
    done = Signal(bool, str, dict)

    def __init__(self, app_key: str, secret_key: str, redirect_uri: str):
        super().__init__()
        self.app_key = app_key
        self.secret_key = secret_key
        self.redirect_uri = redirect_uri
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            ok, msg, tok = baimod.login(
                self.app_key,
                self.secret_key,
                self.redirect_uri,
                log=self.log.emit,
                stop=lambda: self._stop,
            )
        except Exception as exc:  # noqa: BLE001
            ok, msg, tok = False, f"授权出错：{exc}", {}
        self.done.emit(bool(ok), msg, tok or {})


# ---------------------------------------------------------------- 补传线程
class UploadWorker(QThread):
    """把本地 downloads 目录里已有的作品补传到网盘（授权后一次性用）。"""

    log = Signal(str)
    progress = Signal(int, int, str)
    done = Signal(int, int)          # 成功数, 失败数

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        root = Path(self.cfg.get("download_dir") or "")
        b = self.cfg.get("baidu") or {}
        # 两个板块各建一个网盘客户端，补传时按目录层级自动分流
        pans = {
            kind: baimod.BaiduPan(
                mode=b.get("mode", "copy"),
                sync_dir=b.get("sync_dir", ""),
                access_token=b.get("access_token", ""),
                remote_dir=pan_remote_dir(self.cfg, kind),
                log=self.log.emit,
                app_name=b.get("app_name", ""),
                app_key=b.get("app_key", ""),
                secret_key=b.get("secret_key", ""),
                refresh_token=b.get("refresh_token", ""),
                expires_at=b.get("expires_at", 0),
                redirect_uri=b.get("redirect_uri", ""),
            )
            for kind in ("collect", "link")
        }
        jobs: list[tuple[Path, str, str]] = []
        names = collect_folder_names(self.cfg, root)
        try:
            for work in iter_works(root):
                rel = work.relative_to(root)
                kind = upload_kind_for(rel, names)
                sub = rel.as_posix()
                for f in sorted(work.iterdir()):
                    if f.is_file() and not f.name.endswith(".part"):
                        jobs.append((f, sub, kind))
        except OSError as exc:
            self.log.emit(f"读本地目录失败：{exc}")
            self.done.emit(0, 0)
            return

        if not jobs:
            self.log.emit("本地没有需要补传的文件")
            self.done.emit(0, 0)
            return

        n_col = sum(1 for _, _, k in jobs if k == "collect")
        self.log.emit(
            f"准备补传 {len(jobs)} 个文件：收藏板块 {n_col} 个 → 网盘「{pan_remote_dir(self.cfg, 'collect')}」，"
            f"下载板块 {len(jobs) - n_col} 个 → 网盘「{pan_remote_dir(self.cfg, 'link')}」"
        )
        ok = fail = 0
        for i, (f, sub, kind) in enumerate(jobs, 1):
            if self._stop:
                self.log.emit("已停止补传")
                break
            self.progress.emit(i - 1, len(jobs), f.name)
            try:
                pans[kind].save(f, sub)
                ok += 1
                self.log.emit(f"✓ 已传 {pan_remote_dir(self.cfg, kind)}/{sub}/{f.name}")
            except Exception as exc:  # noqa: BLE001
                fail += 1
                self.log.emit(f"× 传不上去 {f.name}：{exc}")
        self.progress.emit(len(jobs), len(jobs), "补传完成")
        self.done.emit(ok, fail)


# ---------------------------------------------------------------- 登录线程
class LoginWorker(QThread):
    log = Signal(str)
    done = Signal(bool)

    def __init__(self, prof: str):
        super().__init__()
        self.prof = Path(prof)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            ok = qr_login(self.prof, log=self.log.emit, stop=lambda: self._stop)
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"登录出错：{exc}")
            ok = False
        self.done.emit(bool(ok))


# ---------------------------------------------------------------- 界面
QSS = """
QWidget { font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; font-size: 13px; color: #1F2329; }
#root { background: #F4F5F7; }
#card { background: #FFFFFF; border: 1px solid #E6E8EB; border-radius: 12px; }
#h1 { font-size: 19px; font-weight: 600; }
#sub { color: #8A9099; font-size: 12px; }
#h2 { font-size: 13px; font-weight: 600; color: #1F2329; }
#hint { color: #8A9099; font-size: 12px; }
QLabel#tag { color: #FE2C55; background: #FFF0F3; border-radius: 6px; padding: 2px 8px; font-size: 12px; }
QLabel#tree {
    color: #4A5159; background: #FAFBFC; border: 1px solid #EDEFF2; border-radius: 8px;
    padding: 8px 10px; font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px;
}

QPlainTextEdit#input {
    background: #FAFBFC; border: 1px solid #E6E8EB; border-radius: 8px; padding: 8px;
    selection-background-color: #FFD6DF; font-size: 13px;
}
QPlainTextEdit#input:focus { border: 1px solid #FE2C55; background: #FFFFFF; }

QTextEdit#log {
    background: #12141A; color: #D7DBE3; border: 1px solid #22262F; border-radius: 8px;
    padding: 8px; font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px;
}

QPushButton { background: #FFFFFF; border: 1px solid #DCE0E5; border-radius: 8px; padding: 7px 14px; }
QPushButton:hover { background: #F7F8FA; border-color: #C9CFD6; }
QPushButton:pressed { background: #EFF1F4; }
QPushButton:disabled { color: #B6BCC4; background: #F5F6F8; border-color: #E6E8EB; }

QPushButton#primary {
    background: #FE2C55; color: #FFFFFF; border: none; font-weight: 600; padding: 9px 26px; font-size: 14px;
}
QPushButton#primary:hover { background: #F0204A; }
QPushButton#primary:disabled { background: #F3B9C4; color: #FFFFFF; }

QTabWidget::pane { border: none; background: transparent; top: -1px; }
QTabBar::tab {
    background: #FFFFFF; border: 1px solid #E6E8EB; border-bottom: none;
    border-top-left-radius: 10px; border-top-right-radius: 10px;
    padding: 9px 20px; margin-right: 6px; color: #5A6169; font-size: 13px;
}
QTabBar::tab:selected { color: #FE2C55; font-weight: 600; }
QTabBar::tab:hover { color: #FE2C55; }

QPushButton#ghost { background: transparent; border: none; color: #5A6270; padding: 6px 8px; }
QPushButton#ghost:hover { color: #FE2C55; background: #FFF0F3; border-radius: 6px; }

QLineEdit { background: #FAFBFC; border: 1px solid #E6E8EB; border-radius: 6px; padding: 5px 8px; color: #4E5661; }
QSpinBox { background: #FAFBFC; border: 1px solid #E6E8EB; border-radius: 6px; padding: 4px 6px; min-width: 64px; }
QComboBox { background: #FAFBFC; border: 1px solid #E6E8EB; border-radius: 6px; padding: 4px 8px; min-width: 150px; }
QComboBox::drop-down { border: none; width: 18px; }
QCheckBox { spacing: 6px; color: #3F4652; }
QCheckBox::indicator { width: 15px; height: 15px; border-radius: 4px; border: 1px solid #C9CFD6; background: #FFFFFF; }
QCheckBox::indicator:checked { background: #FE2C55; border-color: #FE2C55; }

QProgressBar { background: #EEF0F3; border: none; border-radius: 4px; height: 8px; text-align: center; }
QProgressBar::chunk { background: #FE2C55; border-radius: 4px; }
QProgressBar#idle::chunk { background: #E3E6EA; }
"""


def card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    return f


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = cfgmod.load()
        self.worker: Worker | None = None
        self.login_worker: LoginWorker | None = None
        self.sync_worker: SyncWorker | None = None
        self.reclassify_worker: ReclassifyWorker | None = None
        self.pan_worker: PanLoginWorker | None = None
        self.upload_worker: UploadWorker | None = None
        self._loading = False
        self._tray_tip_shown = False

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(900, 800)
        self.resize(980, 960)
        ico = ensure_icon_file()
        if ico:
            self.setWindowIcon(QIcon(str(ico)))

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        self.setStyleSheet(QSS)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        lay.addLayout(self._header())
        lay.addWidget(self._tabs())
        lay.addWidget(self._options_card())
        lay.addWidget(self._disk_card())
        lay.addWidget(self._log_card(), 1)
        lay.addLayout(self._footer())
        self._sync_disk_widgets()

        # 托盘 + 定时同步
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._auto_tick)
        self._init_tray()

    # ---------------- 顶部
    def _header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(make_pixmap(44))
        icon.setFixedSize(44, 44)
        row.addWidget(icon)

        box = QVBoxLayout()
        box.setSpacing(2)
        t = QLabel("抖音收藏助手")
        t.setObjectName("h1")
        s = QLabel("① 收藏自动备份（收藏 + 点赞，按收藏夹分类）　② 分享链接下载　→　本地 + 百度网盘")
        s.setObjectName("sub")
        box.addWidget(t)
        box.addWidget(s)
        row.addLayout(box)
        row.addStretch(1)
        self.lb_login = QLabel("检测中…")
        self.lb_login.setObjectName("hint")
        row.addWidget(self.lb_login)
        self.btn_login = QPushButton("扫码登录")
        self.btn_login.setToolTip(
            "抖音未登录时，收藏夹和博主主页都只能拿到很少的内容（博主主页只有前 18 条）。\n"
            "点这里会弹出浏览器窗口，用抖音 App「扫一扫」登录一次即可抓全。\n"
            "登录状态只保存在本机，不会上传到任何地方。"
        )
        self.btn_login.clicked.connect(self._login)
        row.addWidget(self.btn_login)
        tag = QLabel("免登录可用")
        tag.setObjectName("tag")
        row.addWidget(tag)
        return row

    # ---------------- 链接输入
    def _input_card(self) -> QFrame:
        c = card()
        v = QVBoxLayout(c)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        top = QHBoxLayout()
        h = QLabel("分享链接")
        h.setObjectName("h2")
        top.addWidget(h)
        self.count_label = QLabel("0 个链接")
        self.count_label.setObjectName("hint")
        top.addWidget(self.count_label)
        top.addStretch(1)
        for text, slot in (("从剪贴板粘贴", self._paste), ("清空", self._clear_input)):
            b = QPushButton(text)
            b.setObjectName("ghost")
            b.clicked.connect(slot)
            top.addWidget(b)
        v.addLayout(top)

        self.input = QPlainTextEdit()
        self.input.setObjectName("input")
        self.input.setPlaceholderText(
            "把抖音分享链接或整段分享文案粘进来，一行一个：\n"
            "  https://v.douyin.com/xxxxxxx/     ← 单条视频 / 图文\n"
            "  https://www.douyin.com/user/MS4wLjAB...   ← 这个博主的全部作品"
        )
        self.input.setFixedHeight(80)
        self.input.textChanged.connect(self._update_count)
        v.addWidget(self.input)
        return c

    # ---------------- 选项
    def _options_card(self) -> QFrame:
        c = card()
        v = QVBoxLayout(c)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)

        h = QLabel("下载选项")
        h.setObjectName("h2")
        v.addWidget(h)

        row = QHBoxLayout()
        row.setSpacing(18)
        self.cb_images = QCheckBox("保存无水印图片")
        self.cb_video = QCheckBox("图文合成视频（带背景音乐）")
        self.cb_bgm = QCheckBox("另存背景音乐 mp3")
        self.cb_bgm.setToolTip(
            "只影响要不要单独留一份 mp3 文件。\n"
            "合成出来的视频始终带背景音乐，取消勾选也不会变成无声。"
        )
        for cb in (self.cb_images, self.cb_video, self.cb_bgm):
            row.addWidget(cb)
        row.addStretch(1)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        lb_sec = QLabel("每张图停留")
        lb_sec.setObjectName("hint")
        row2.addWidget(lb_sec)
        self.spin_sec = QDoubleSpinBox()
        self.spin_sec.setRange(0.8, 10.0)
        self.spin_sec.setSingleStep(0.2)
        self.spin_sec.setDecimals(1)
        self.spin_sec.setSuffix(" 秒")
        self.spin_sec.setFixedWidth(84)
        self.spin_sec.setToolTip(
            "图文合成时每张图停留的时间。\n"
            "总时长以背景音乐为准：音乐比图片长时图片自动循环，\n"
            "音乐放完即结束，不会再被切断。"
        )
        row2.addWidget(self.spin_sec)
        row2.addSpacing(16)
        lb = QLabel("博主主页最多抓")
        lb.setObjectName("hint")
        row2.addWidget(lb)
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(0, 9999)
        self.spin_limit.setSpecialValueText("全部")
        row2.addWidget(self.spin_limit)
        lb2 = QLabel("条（0 = 全部作品）")
        lb2.setObjectName("hint")
        row2.addWidget(lb2)
        row2.addStretch(1)
        v.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(8)
        lb_dup = QLabel("已下载过的作品")
        lb_dup.setObjectName("hint")
        row3.addWidget(lb_dup)
        self.cmb_dup = QComboBox()
        self.cmb_dup.addItems(["跳过，不重复下载", "重新下载并覆盖", "每条都问一下我"])
        self.cmb_dup.currentIndexChanged.connect(self._dup_changed)
        self.cmb_dup.setToolTip(
            "同一个作品再下一次时怎么处理：\n"
            "· 跳过：按 .downloaded.json 里记录的作品 ID 直接跳过，最省时间\n"
            "· 重新下载：删掉旧文件重新拉一遍（想换清晰度、修旧文件时用）\n"
            "· 每条问一下：每条弹窗，可以选「后面全部」一次决定"
        )
        row3.addWidget(self.cmb_dup)
        lb_dup_hint = QLabel("覆盖同名旧文件")
        lb_dup_hint.setObjectName("hint")
        row3.addWidget(lb_dup_hint)
        row3.addSpacing(18)
        self.cb_show_browser = QCheckBox("遇到验证码时显示浏览器")
        row3.addWidget(self.cb_show_browser)
        row3.addStretch(1)
        v.addLayout(row3)
        return c

    # ---------------- 保存位置
    def _disk_card(self) -> QFrame:
        c = card()
        self.disk_card = c
        v = QVBoxLayout(c)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)

        h = QLabel("保存位置")
        h.setObjectName("h2")
        v.addWidget(h)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        n1 = QLabel("本地目录")
        n1.setFixedWidth(64)
        n1.setObjectName("hint")
        r1.addWidget(n1)
        self.ed_local = QLineEdit()
        self.ed_local.setReadOnly(True)
        r1.addWidget(self.ed_local, 1)
        b1 = QPushButton("更改")
        b1.clicked.connect(self._pick_local)
        r1.addWidget(b1)
        v.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        n2 = QLabel("网盘方式")
        n2.setFixedWidth(64)
        n2.setObjectName("hint")
        r2.addWidget(n2)
        self.cb_mode = QComboBox()
        self.cb_mode.addItems(["同步文件夹（客户端自动上传）", "仅存本地，不上传", "开放平台 API 直传"])
        self.cb_mode.currentIndexChanged.connect(self._mode_changed)
        r2.addWidget(self.cb_mode)
        r2.addStretch(1)
        b2 = QPushButton("打开下载目录")
        b2.clicked.connect(self._open_dir)
        r2.addWidget(b2)
        v.addLayout(r2)

        self.disk_row = QWidget()
        r3 = QHBoxLayout(self.disk_row)
        r3.setContentsMargins(0, 0, 0, 0)
        r3.setSpacing(8)
        n3 = QLabel("同步目录")
        n3.setFixedWidth(64)
        n3.setObjectName("hint")
        r3.addWidget(n3)
        self.ed_sync = QLineEdit()
        self.ed_sync.setReadOnly(True)
        self.ed_sync.setPlaceholderText("选百度网盘客户端的同步文件夹，文件丢进去会自动上传")
        r3.addWidget(self.ed_sync, 1)
        b3 = QPushButton("选择")
        b3.clicked.connect(self._pick_sync)
        r3.addWidget(b3)
        v.addWidget(self.disk_row)

        self.api_row = QWidget()
        av = QVBoxLayout(self.api_row)
        av.setContentsMargins(0, 0, 0, 0)
        av.setSpacing(7)

        r4 = QHBoxLayout()
        r4.setSpacing(8)
        n4 = QLabel("AppKey")
        n4.setFixedWidth(64)
        n4.setObjectName("hint")
        r4.addWidget(n4)
        self.ed_akey = QLineEdit()
        self.ed_akey.setPlaceholderText("开放平台应用的 AppKey")
        self.ed_akey.editingFinished.connect(self._after_save)
        r4.addWidget(self.ed_akey, 1)
        lb_sk = QLabel("SecretKey")
        lb_sk.setObjectName("hint")
        r4.addWidget(lb_sk)
        self.ed_skey = QLineEdit()
        self.ed_skey.setEchoMode(QLineEdit.Password)
        self.ed_skey.setPlaceholderText("应用的 SecretKey")
        self.ed_skey.editingFinished.connect(self._after_save)
        r4.addWidget(self.ed_skey, 1)
        self.lb_pan = QLabel("未授权")
        self.lb_pan.setObjectName("hint")
        self.lb_pan.setMinimumWidth(140)
        r4.addWidget(self.lb_pan)
        self.btn_pan = QPushButton("授权百度网盘")
        self.btn_pan.setToolTip(
            "点一下会打开百度授权页，用你的百度账号登录并点「授权」。\n"
            "授权后软件就能直接把文件传到网盘，不用装百度网盘客户端。\n"
            "授权有效期 30 天，过期前会自动续期。"
        )
        self.btn_pan.clicked.connect(self._pan_login)
        r4.addWidget(self.btn_pan)
        av.addLayout(r4)

        r6 = QHBoxLayout()
        r6.setSpacing(8)
        n6 = QLabel("网盘目录")
        n6.setFixedWidth(64)
        n6.setObjectName("hint")
        r6.addWidget(n6)
        lb_c = QLabel("收藏备份")
        lb_c.setObjectName("hint")
        r6.addWidget(lb_c)
        self.ed_folder_collect = QLineEdit()
        self.ed_folder_collect.setFixedWidth(118)
        self.ed_folder_collect.setToolTip(
            "「收藏自动备份」板块传到这里：我的应用数据 / 这个目录 / <收藏夹>/<作者>/<作品>"
        )
        self.ed_folder_collect.editingFinished.connect(self._after_save)
        r6.addWidget(self.ed_folder_collect)
        r6.addSpacing(14)
        lb_l = QLabel("视频下载")
        lb_l.setObjectName("hint")
        r6.addWidget(lb_l)
        self.ed_folder_link = QLineEdit()
        self.ed_folder_link.setFixedWidth(118)
        self.ed_folder_link.setToolTip(
            "「视频下载」板块传到这里：我的应用数据 / 这个目录 / <作者>/<作品>"
        )
        self.ed_folder_link.editingFinished.connect(self._after_save)
        r6.addWidget(self.ed_folder_link)
        self.lb_pan_paths = QLabel("")
        self.lb_pan_paths.setObjectName("hint")
        r6.addWidget(self.lb_pan_paths)
        r6.addStretch(1)
        av.addLayout(r6)

        lb_tip = QLabel(
            "开放平台回调地址填 http://localhost:8899/callback；两个板块各自上传，互不混在一起。"
        )
        lb_tip.setObjectName("hint")
        lb_tip.setWordWrap(True)
        av.addWidget(lb_tip)

        v.addWidget(self.api_row)
        return c

    # ---------------- 板块一：收藏自动备份
    def _collect_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(0, 10, 0, 0)
        outer.setSpacing(10)

        c = card()
        v = QVBoxLayout(c)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)

        top = QHBoxLayout()
        h = QLabel("收藏 + 点赞 自动备份")
        h.setObjectName("h2")
        top.addWidget(h)
        top.addStretch(1)
        self.lb_sync = QLabel("未开启")
        self.lb_sync.setObjectName("hint")
        top.addWidget(self.lb_sync)
        v.addLayout(top)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.cb_auto = QCheckBox("开启自动同步")
        self.cb_auto.setToolTip(
            "开启后，软件会每隔一段时间自动去抖音查一次「我的收藏」，\n"
            "发现没下过的作品就下载、合成、传网盘。\n"
            "手机在抖音里点收藏即可，电脑需要开机并让软件在后台运行。"
        )
        self.cb_auto.stateChanged.connect(self._auto_changed)
        r1.addWidget(self.cb_auto)
        r1.addWidget(QLabel("每"))
        self.cmb_interval = QComboBox()
        for m in (5, 10, 15, 30, 60, 180):
            self.cmb_interval.addItem(f"{m} 分钟", m)
        self.cmb_interval.currentIndexChanged.connect(self._auto_changed)
        r1.addWidget(self.cmb_interval)
        r1.addWidget(QLabel("查一次"))
        r1.addSpacing(14)
        r1.addWidget(QLabel("范围"))
        self.cmb_scope = QComboBox()
        self.cmb_scope.addItems(["收藏 + 点赞（都点过的）", "只备份收藏", "只备份点赞"])
        self.cmb_scope.setToolTip(
            "收藏 + 点赞（都点过的）：只备份**既收藏了、又点了赞**的作品，两个都点过才备份；\n"
            "只收藏没点赞的、只点赞没收藏的，都不备份\n"
            "只备份收藏：所有收藏都备份，严格按抖音收藏夹分类\n"
            "只备份点赞：只备份你点「喜欢」的作品，有收藏夹的归收藏夹，其余的放「视频」文件夹"
        )
        self.cmb_scope.currentIndexChanged.connect(self._auto_changed)
        self.cmb_scope.currentIndexChanged.connect(self._update_scope_hint)
        r1.addWidget(self.cmb_scope)
        r1.addStretch(1)
        self.btn_sync_now = QPushButton("立即备份一次")
        self.btn_sync_now.setObjectName("primary")
        self.btn_sync_now.setMinimumHeight(34)
        self.btn_sync_now.clicked.connect(self._sync_now)
        r1.addWidget(self.btn_sync_now)
        v.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        self.cb_by_folder = QCheckBox("按抖音收藏夹分文件夹")
        self.cb_by_folder.setToolTip(
            "勾上以后目录长这样（直接按抖音收藏夹分，不再套一层作者）：\n"
            "  抖音收藏/古风/20240101_标题_123456/\n"
            "  抖音收藏/AIGC/20240102_标题_654321/\n"
            "  抖音收藏/视频/20240103_标题_789012/   ← 收藏/点赞了但没归到任何收藏夹的\n"
            "不勾就还是：抖音收藏/作者/作品/"
        )
        self.cb_by_folder.stateChanged.connect(self._auto_changed)
        r2.addWidget(self.cb_by_folder)
        r2.addStretch(1)
        self.btn_upload = QPushButton("补传本地文件到网盘")
        self.btn_upload.setToolTip(
            "把本地已经下好的作品，按现在的目录结构补传到网盘「收藏备份目录」，只传网盘上还没有的文件。"
        )
        self.btn_upload.clicked.connect(self._upload_existing)
        r2.addWidget(self.btn_upload)
        self.btn_reclassify = QPushButton("整理已有备份")
        self.btn_reclassify.setToolTip(
            "把之前已经下好的作品，按抖音收藏夹重新分文件夹（只移动文件，不会重新下载）。\n"
            "需要先扫码登录，并且会花几分钟把收藏夹扫一遍。"
        )
        self.btn_reclassify.clicked.connect(self._reclassify)
        r2.addWidget(self.btn_reclassify)
        self.btn_reconcile = QPushButton("对照网盘补漏")
        self.btn_reconcile.setToolTip(
            "把每个收藏夹翻到底，跟网盘「收藏备份目录」里已有的作品对账：\n"
            "网盘上缺的就补——本地有文件直接补传，本地也没有就重新下载再上传。\n"
            "适合删过备份、换过电脑、或怀疑漏传的时候跑一次（要几分钟）。"
        )
        self.btn_reconcile.clicked.connect(self._reconcile_pan)
        r2.addWidget(self.btn_reconcile)
        v.addLayout(r2)

        hint = QLabel(
            "手机点收藏 / 点赞 → 电脑到点自动查（不会重复下载）→ 有收藏夹的进对应收藏夹、"
            "没有收藏夹的统一进「视频」文件夹 → 传网盘「我的应用数据 / 收藏备份目录」。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        # 提示随范围变化：交集模式容易被误解成并集，这里必须说清楚
        self.lb_collect_hint = hint
        self._update_scope_hint()

        outer.addWidget(c)
        outer.addStretch(1)
        return w

    # ---------------- 板块二：视频下载
    def _link_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(0, 10, 0, 0)
        outer.setSpacing(10)
        outer.addWidget(self._input_card())

        row = QHBoxLayout()
        row.setSpacing(10)
        lb = QLabel(
            "单条链接 → 存「作者/作品」；博主主页 → 存这个博主的全部作品。"
            "下好会自动传到网盘「我的应用数据 / 视频下载目录」。"
        )
        lb.setObjectName("hint")
        lb.setWordWrap(True)
        row.addWidget(lb, 1)
        self.btn_start = QPushButton("开始下载")
        self.btn_start.setObjectName("primary")
        self.btn_start.setMinimumHeight(36)
        self.btn_start.clicked.connect(self._start)
        row.addWidget(self.btn_start)
        outer.addLayout(row)
        outer.addStretch(1)
        return w

    # ---------------- 两个板块
    def _tabs(self) -> QWidget:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._collect_tab(), "① 收藏自动备份")
        self.tabs.addTab(self._link_tab(), "② 视频下载")
        return self.tabs

    # ---------------- 日志
    def _log_card(self) -> QFrame:
        c = card()
        v = QVBoxLayout(c)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        top = QHBoxLayout()
        h = QLabel("运行日志")
        h.setObjectName("h2")
        top.addWidget(h)
        top.addStretch(1)
        b = QPushButton("清空")
        b.setObjectName("ghost")
        b.clicked.connect(lambda: self.log_view.clear())
        top.addWidget(b)
        v.addLayout(top)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.WidgetWidth)
        # 日志区是可伸缩的那块：最小高度放松一点，别把上面的两个板块挤扁
        self.log_view.setMinimumHeight(110)
        v.addWidget(self.log_view, 1)
        return c

    # ---------------- 底部
    def _footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(6)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        left.addWidget(self.progress)
        self.status = QLabel("等待开始")
        self.status.setObjectName("hint")
        left.addWidget(self.status)
        row.addLayout(left, 1)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setMinimumHeight(34)
        self.btn_stop.clicked.connect(self._stop)
        row.addWidget(self.btn_stop)
        return row

    # ---------------- 数据同步
    def load_from_cfg(self) -> None:
        self._loading = True
        c = self.cfg
        self.ed_local.setText(c.get("download_dir", ""))
        b = c.get("baidu") or {}
        mode = b.get("mode", "copy")
        self.cb_mode.setCurrentIndex({"copy": 0, "off": 1, "api": 2}.get(mode, 0))
        self.ed_sync.setText(b.get("sync_dir", "") or "")
        self.ed_akey.setText(b.get("app_key", "") or "")
        self.ed_skey.setText(b.get("secret_key", "") or "")
        self.ed_folder_collect.setText(pan_remote_dir(c, "collect"))
        self.ed_folder_link.setText(pan_remote_dir(c, "link"))
        img = c.get("images") or {}
        self.cb_images.setChecked(bool(img.get("save_images", True)))
        self.cb_video.setChecked(bool(img.get("make_video", True)))
        self.cb_bgm.setChecked(bool(img.get("save_bgm", True)))
        self.spin_sec.setValue(float(img.get("image_seconds", 2.6) or 2.6))
        self.spin_limit.setValue(int(c.get("max_user_videos", 0) or 0))
        self.cmb_dup.setCurrentIndex({"skip": 0, "overwrite": 1, "ask": 2}.get(str(c.get("duplicate", "skip")), 0))

        auto = c.get("auto_sync") or {}
        self.cb_auto.setChecked(bool(auto.get("enabled", False)))
        iv = int(auto.get("interval_minutes", 10) or 10)
        idx = self.cmb_interval.findData(iv)
        self.cmb_interval.setCurrentIndex(idx if idx >= 0 else 1)
        self.cmb_scope.setCurrentIndex(
            {"both": 0, "collect": 1, "like": 2}.get(str(auto.get("scope", "both")), 0)
        )
        org = c.get("organize") or {}
        self.cb_by_folder.setChecked(bool(org.get("by_collect_folder", True)))

        self._sync_disk_widgets()
        self._loading = False
        self._refresh_login_label()
        self._refresh_pan_label()
        self._apply_timer()

    def _after_save(self) -> None:
        if not getattr(self, "_loading", False):
            self._save_cfg()
            self._refresh_pan_label()

    def _save_cfg(self) -> None:
        c = self.cfg
        c["download_dir"] = self.ed_local.text().strip() or c["download_dir"]
        b = dict(c.get("baidu") or {})
        mode = ["copy", "off", "api"][self.cb_mode.currentIndex()]
        b["mode"] = mode
        b["enabled"] = mode != "off"
        b["sync_dir"] = self.ed_sync.text().strip()
        b["app_key"] = self.ed_akey.text().strip()
        b["secret_key"] = self.ed_skey.text().strip()
        b["remote_dir_collect"] = self.ed_folder_collect.text().strip() or "抖音收藏"
        b["remote_dir_link"] = self.ed_folder_link.text().strip() or "视频备份"
        # access_token / refresh_token / expires_at 由授权流程维护，这里不动
        c["baidu"] = b
        img = dict(c.get("images") or {})
        img.update(
            {
                "save_images": self.cb_images.isChecked(),
                "make_video": self.cb_video.isChecked(),
                "save_bgm": self.cb_bgm.isChecked(),
                "image_seconds": float(self.spin_sec.value()),
            }
        )
        c["images"] = img
        auto = dict(c.get("auto_sync") or {})
        auto["enabled"] = self.cb_auto.isChecked()
        auto["interval_minutes"] = int(self.cmb_interval.currentData() or 10)
        auto["scope"] = ["both", "collect", "like"][self.cmb_scope.currentIndex()]
        c["auto_sync"] = auto
        org = dict(c.get("organize") or {})
        org["by_collect_folder"] = self.cb_by_folder.isChecked()
        c["organize"] = org
        c["max_user_videos"] = self.spin_limit.value()
        c["duplicate"] = ["skip", "overwrite", "ask"][self.cmb_dup.currentIndex()]
        self.cfg = cfgmod.save(c)

    def _dup_changed(self) -> None:
        if not getattr(self, "_loading", False):
            self._save_cfg()

    # ---------------- 自动同步
    def _busy(self) -> bool:
        for w in (self.worker, self.login_worker, self.sync_worker,
                  self.reclassify_worker, self.upload_worker):
            if w and w.isRunning():
                return True
        return False

    def _update_scope_hint(self) -> None:
        """按当前「范围」把说明换掉 —— 交集最容易被误解成并集。"""
        lb = getattr(self, "lb_collect_hint", None)
        if lb is None:
            return
        idx = self.cmb_scope.currentIndex()
        if idx == 0:
            txt = (
                "范围「收藏 + 点赞（都点过的）」= 只备份既收藏了、又点了赞的作品；只收藏没点赞、"
                "只点赞没收藏的都不备份。有收藏夹的进对应收藏夹（收藏夹 / 作品 /），"
                "没收藏夹的进「视频 / 作品 /」。"
            )
        elif idx == 1:
            txt = (
                "只备份收藏：只要收藏了的都备份。有收藏夹的进对应收藏夹（收藏夹 / 作品 /），"
                "没归到任何收藏夹的进「视频 / 作品 /」。"
            )
        else:
            txt = (
                "只备份点赞：只备份你点过「喜欢」的作品。本来就在某个收藏夹里的归到那个收藏夹，"
                "其余的放「视频 / 作品 /」。"
            )
        lb.setText(txt.replace("**", ""))

    def _auto_changed(self) -> None:
        if getattr(self, "_loading", False):
            return
        self._save_cfg()
        self._apply_timer()

    def _apply_timer(self) -> None:
        auto = self.cfg.get("auto_sync") or {}
        if auto.get("enabled") and getattr(self, "tray", None):
            mins = max(1, int(auto.get("interval_minutes", 10) or 10))
            self.timer.start(mins * 60 * 1000)
            self.lb_sync.setText(f"已开启 · 每 {mins} 分钟查一次")
        else:
            self.timer.stop()
            self.lb_sync.setText("未开启")

    def _auto_tick(self) -> None:
        if self._busy():
            return
        self._append_log("—— 自动检查手机上的收藏 ——")
        self._sync_now(auto=True)

    def _sync_now(self, auto: bool = False) -> None:
        if self._busy():
            if not auto:
                QMessageBox.information(self, "先等一下", "现在有任务在跑，等它结束再同步。")
            return
        self._save_cfg()
        scope = ["both", "collect", "like"][self.cmb_scope.currentIndex()]
        for b in (self.btn_sync_now, self.btn_reclassify, self.btn_start, self.btn_reconcile):
            b.setEnabled(False)
        self.progress.setRange(0, 0)
        self.status.setText("正在检查收藏夹…")
        self.lb_sync.setText("同步中…")
        w = SyncWorker(
            self.cfg,
            headless=not self.cb_show_browser.isChecked(),
            scope=scope,
            full=bool((self.cfg.get("auto_sync") or {}).get("full_scan")),
            reconcile=False,        # 日常同步不对账，对账走「对照网盘补漏」
        )
        w.log.connect(self._append_log)
        w.progress.connect(self._on_progress)
        w.done.connect(self._on_sync_done)
        self.sync_worker = w
        w.start()

    def _reconcile_pan(self) -> None:
        """对照网盘补漏：收藏夹全量扫一遍，缺什么补什么。"""
        if self._busy():
            QMessageBox.information(self, "先等一下", "现在有任务在跑，等它结束再对账。")
            return
        ok = QMessageBox.question(
            self, "对照网盘补漏",
            "会把每个收藏夹翻到底，跟网盘上已有的作品一一对账，缺的就补。\n"
            "要花几分钟，现在开始吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ok != QMessageBox.Yes:
            return
        self._save_cfg()
        scope = ["both", "collect", "like"][self.cmb_scope.currentIndex()]
        for b in (self.btn_sync_now, self.btn_reclassify, self.btn_start, self.btn_reconcile):
            b.setEnabled(False)
        self.progress.setRange(0, 0)
        self.status.setText("正在对照网盘补漏…")
        w = SyncWorker(
            self.cfg,
            headless=not self.cb_show_browser.isChecked(),
            scope=scope,
            full=True,
            reconcile=True,
        )
        w.log.connect(self._append_log)
        w.progress.connect(self._on_progress)
        w.done.connect(self._on_sync_done)
        self.sync_worker = w
        w.start()

    def _reclassify(self) -> None:
        """把已有的旧备份按抖音收藏夹分好文件夹。"""
        if self._busy():
            QMessageBox.information(self, "先等一下", "现在有任务在跑，等它结束再整理。")
            return
        ok = QMessageBox.question(
            self,
            "整理已有备份",
            "会把已经下好的作品，按抖音里的收藏夹重新分文件夹。\n\n"
            "· 只移动文件，不会重新下载、不会删除\n"
            "· 需要先把收藏夹扫一遍，67 个收藏夹大概几分钟\n"
            "· 网盘同步文件夹里的文件也会一起整理\n\n要开始吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if ok != QMessageBox.Yes:
            return
        self._save_cfg()
        self.btn_sync_now.setEnabled(False)
        self.btn_reclassify.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.progress.setRange(0, 0)
        self.status.setText("正在读取收藏夹…")
        self._append_log("—— 开始整理已有备份（按收藏夹分类）——")
        w = ReclassifyWorker(self.cfg, headless=not self.cb_show_browser.isChecked())
        w.log.connect(self._append_log)
        w.progress.connect(self._on_progress)
        w.done.connect(self._on_reclassify_done)
        self.reclassify_worker = w
        w.start()

    def _on_reclassify_done(self, got: dict) -> None:
        self.btn_sync_now.setEnabled(True)
        self.btn_reclassify.setEnabled(True)
        self.btn_start.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        moved = int(got.get("moved") or 0)
        unknown = int(got.get("unknown") or 0)
        if got.get("error"):
            self.status.setText(f"整理失败：{str(got['error'])[:60]}")
        elif moved:
            self.status.setText(
                f"整理完成：{moved} 个作品已归到对应收藏夹"
                + (f"，{unknown} 个在收藏夹里找不到" if unknown else "")
            )
        else:
            self.status.setText("没有需要移动的作品" + (f"（{unknown} 个在收藏夹里找不到）" if unknown else ""))

    def _on_sync_done(self, got: dict) -> None:
        self.btn_sync_now.setEnabled(True)
        self.btn_reclassify.setEnabled(True)
        self.btn_reconcile.setEnabled(True)
        self.btn_start.setEnabled(True)
        repaired = int(got.get("repaired") or 0)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        n = int(got.get("new") or 0)
        total = int(got.get("total") or 0)
        skipped = int(got.get("skipped") or 0)
        filtered = int(got.get("filtered") or 0)
        flt = f"，{filtered} 条只收藏没点赞已跳过" if filtered else ""
        stamp = time.strftime("%H:%M")
        if got.get("error"):
            self.status.setText(f"同步失败：{str(got['error'])[:60]}")
            self.lb_sync.setText("上次同步出错")
        elif n:
            rep_txt = f"，补漏 {repaired} 条" if repaired else ""
            self.status.setText(f"同步完成：新增 {n} 条（候选 {total} 条{flt}{rep_txt}）")
            self.lb_sync.setText(f"上次新增 {n} 条 · {stamp}")
        else:
            self.status.setText(f"没有新作品（候选 {total} 条，已备份 {skipped} 条{flt}）")
            self.lb_sync.setText(f"上次无新增 · {stamp}")
        self._apply_timer()
        if self.isHidden() and getattr(self, "tray", None) and n:
            self.tray.showMessage(APP_NAME, f"自动备份了 {n} 条新作品", QSystemTrayIcon.Information, 4000)

    # ---------------- 补传本地已有文件
    def _local_file_count(self) -> int:
        root = Path(self.cfg.get("download_dir") or "")
        n = 0
        try:
            for work in iter_works(root):
                n += sum(1 for f in work.iterdir() if f.is_file() and not f.name.endswith(".part"))
        except OSError:
            return 0
        return n

    def _upload_existing(self) -> None:
        self._save_cfg()
        b = self.cfg.get("baidu") or {}
        if not (b.get("access_token") or "").strip():
            QMessageBox.information(self, "先授权", "还没授权百度网盘，先点「授权百度网盘」。")
            return
        if self._busy():
            QMessageBox.information(self, "先等一下", "现在有任务在跑，等它结束再补传。")
            return
        n = self._local_file_count()
        if not n:
            QMessageBox.information(self, "没有文件", "本地下载目录里还没有可补传的作品。")
            return
        self.btn_sync_now.setEnabled(False)
        self.progress.setRange(0, n)
        self.progress.setValue(0)
        self.status.setText(f"正在补传 {n} 个文件…")
        self._append_log(f"—— 开始补传本地文件（{n} 个）——")
        w = UploadWorker(self.cfg)
        w.log.connect(self._append_log)
        w.progress.connect(self._on_progress)
        w.done.connect(self._on_upload_done)
        self.upload_worker = w
        w.start()

    def _on_upload_done(self, ok: int, fail: int) -> None:
        self.btn_sync_now.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        if ok or fail:
            self.status.setText(f"补传完成：成功 {ok} 个" + (f"，失败 {fail} 个" if fail else ""))
        else:
            self.status.setText("没有需要补传的文件")

    # ---------------- 网盘授权
    def _refresh_pan_label(self) -> None:
        b = self.cfg.get("baidu") or {}
        tok = (b.get("access_token") or "").strip()
        exp = float(b.get("expires_at") or 0)
        if tok:
            if exp:
                self.lb_pan.setText("已授权，有效期至 " + time.strftime("%Y-%m-%d", time.localtime(exp)))
            else:
                self.lb_pan.setText("已授权")
            self.btn_pan.setText("重新授权")
        else:
            self.lb_pan.setText("未授权")
            self.btn_pan.setText("授权百度网盘")

        # 顺手把两个板块最终会落到网盘的哪一层显示出来，省得用户猜
        app_name = (b.get("app_name") or "").strip()
        base = f"/apps/{app_name}" if app_name else "/apps"
        self.lb_pan_paths.setText(
            f"→ {base}/{pan_remote_dir(self.cfg, 'collect')}　"
            f"|　{base}/{pan_remote_dir(self.cfg, 'link')}"
        )

    def _pan_login(self) -> None:
        self._save_cfg()
        b = self.cfg.get("baidu") or {}
        ak = (b.get("app_key") or "").strip()
        sk = (b.get("secret_key") or "").strip()
        if not ak or not sk:
            QMessageBox.information(
                self,
                "先去开放平台创建应用",
                "三步，5 分钟：\n\n"
                "1. 打开 pan.baidu.com/union，用百度账号登录，完成「个人开发者」实名认证\n"
                "2. 创建应用：用途选「个人使用」，应用名随便起（例如 抖音收藏助手）\n"
                "3. 在【安全设置】里把「授权回调地址」填成\n"
                "     http://localhost:8899/callback\n\n"
                "然后把页面上给你的 AppKey、SecretKey 和应用名填到上面，再点这个按钮。",
            )
            return
        if self.pan_worker and self.pan_worker.isRunning():
            return
        if self._busy():
            QMessageBox.information(self, "先等一下", "现在有任务在跑，等它结束再授权。")
            return
        self.btn_pan.setEnabled(False)
        self.lb_pan.setText("等待浏览器授权…")
        self._append_log("—— 已打开百度授权页，请登录并点「授权」 ——")
        w = PanLoginWorker(ak, sk, b.get("redirect_uri") or baimod.DEFAULT_REDIRECT)
        w.log.connect(self._append_log)
        w.done.connect(self._on_pan_done)
        self.pan_worker = w
        w.start()

    def _on_pan_done(self, ok: bool, msg: str, tok: dict) -> None:
        self.btn_pan.setEnabled(True)
        if ok and tok.get("access_token"):
            b = dict(self.cfg.get("baidu") or {})
            b["access_token"] = tok["access_token"]
            b["refresh_token"] = tok.get("refresh_token", b.get("refresh_token", ""))
            b["expires_at"] = time.time() + float(tok.get("expires_in") or 2592000)
            self.cfg["baidu"] = b
            self.cfg = cfgmod.save(self.cfg)
            self._append_log("✓ 百度网盘授权成功")
            self.status.setText("百度网盘已授权，之后下载会直接传到网盘")
            n = self._local_file_count()
            if n and QMessageBox.question(
                self,
                "授权成功",
                f"本地已经下载过 {n} 个文件，要现在补传到网盘吗？\n\n"
                "补传只是复制一份上去，本地文件不会动。\n"
                "（以后新收藏的作品会自动传，不用再管）",
            ) == QMessageBox.Yes:
                self._upload_existing()
        else:
            self._append_log(f"授权没成功：{msg}")
            self.status.setText("百度网盘授权未完成")
            QMessageBox.warning(
                self,
                "授权没成功",
                f"{msg}\n\n常见原因：\n"
                "· 开放平台里「授权回调地址」没填 http://localhost:8899/callback\n"
                "· 应用还没通过审核（个人使用类是免审核的）\n"
                "· 浏览器里最后没点「授权」",
            )
        self._refresh_pan_label()

    # ---------------- 托盘
    def _init_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        ico = ensure_icon_file()
        self.tray = QSystemTrayIcon(QIcon(str(ico)) if ico else QIcon(make_pixmap(64)), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu(self)
        a1 = QAction("打开主窗口", self)
        a1.triggered.connect(self._show_window)
        menu.addAction(a1)
        a2 = QAction("立即备份一次（收藏 + 点赞）", self)
        a2.triggered.connect(lambda: self._sync_now())
        menu.addAction(a2)
        a4 = QAction("把本地文件补传到网盘", self)
        a4.triggered.connect(self._upload_existing)
        menu.addAction(a4)
        menu.addSeparator()
        a3 = QAction("退出", self)
        a3.triggered.connect(self._quit_app)
        menu.addAction(a3)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_window()

    def _quit_app(self) -> None:
        self._force_quit = True
        self.close()


    def _sync_disk_widgets(self) -> None:
        i = self.cb_mode.currentIndex()
        self.disk_row.setVisible(i == 0)
        self.api_row.setVisible(i == 2)
        # 切到 API 模式时表单要多出三行，手动让布局重算，否则会被卡片高度裁掉
        self.api_row.updateGeometry()
        if getattr(self, "disk_card", None):
            self.disk_card.adjustSize()
        central = self.centralWidget()
        if central and central.layout():
            central.layout().activate()

    def _mode_changed(self) -> None:
        self._sync_disk_widgets()
        if not getattr(self, "_loading", False):
            self._save_cfg()

    def _pick_local(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择本地下载目录", self.ed_local.text() or str(cfgmod.ROOT))
        if d:
            self.ed_local.setText(str(Path(d)))
            self._save_cfg()

    def _pick_sync(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择百度网盘同步文件夹", self.ed_sync.text() or str(Path.home()))
        if d:
            self.ed_sync.setText(str(Path(d)))
            self._save_cfg()

    def _open_dir(self) -> None:
        d = Path(self.ed_local.text() or str(cfgmod.ROOT))
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", str(exc))

    # ---------------- 交互
    def _paste(self) -> None:
        text = QApplication.clipboard().text().strip()
        if not text:
            return
        cur = self.input.toPlainText().strip()
        self.input.setPlainText((cur + "\n" + text).strip() if cur else text)

    def _clear_input(self) -> None:
        self.input.clear()

    def _update_count(self) -> None:
        n = len([ln for ln in self.input.toPlainText().splitlines() if ln.strip()])
        self.count_label.setText(f"{n} 个链接")

    # ---------------- 扫码登录
    def _prof(self) -> Path:
        return douyin_profile(Path(self.ed_local.text().strip() or str(cfgmod.ROOT)))

    def _refresh_login_label(self) -> None:
        try:
            logged = has_login(self._prof())
        except Exception:  # noqa: BLE001
            logged = False
        if logged:
            self.lb_login.setText("账号：已登录")
            self.btn_login.setText("重新扫码")
        else:
            self.lb_login.setText("账号：未登录（仅前 18 条）")
            self.btn_login.setText("扫码登录")

    def _login(self) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(
                self, "先等一下", "正在下载，等这一轮跑完再扫码登录（浏览器同一时间只能开一个）。"
            )
            return
        if self.login_worker and self.login_worker.isRunning():
            return
        self._save_cfg()
        self.btn_login.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.status.setText("等待扫码登录…（请在浏览器窗口里扫码）")
        self._append_log("—— 打开浏览器，请用抖音 App 扫码登录 ——")

        w = LoginWorker(str(self._prof()))
        w.log.connect(self._append_log)
        w.done.connect(self._on_login_done)
        self.login_worker = w
        w.start()

    def _on_login_done(self, ok: bool) -> None:
        self.btn_login.setEnabled(True)
        self.btn_start.setEnabled(True)
        self.status.setText("登录成功，可以抓博主全部作品了" if ok else "没有登录成功，随时可以再试")
        self._append_log("✓ 登录成功" if ok else "登录未完成")
        self._refresh_login_label()

    def _append_log(self, msg: str) -> None:
        low = msg
        color = "#D7DBE3"
        if "✓" in low:
            color = "#5BD98B"
        elif "×" in low or "失败" in low or "出错" in low:
            color = "#FF7B8A"
        elif "重新下载" in low or "重新合成" in low:
            color = "#FFC46B"
        elif "跳过" in low or "提示" in low:
            color = "#8C93A1"
        elif low.startswith("——") or "解析" in low:
            color = "#7FB4FF"
        self.log_view.moveCursor(QTextCursor.End)
        self.log_view.insertHtml(
            f'<span style="color:{color};white-space:pre-wrap">{html.escape(msg)}</span><br>'
        )
        self.log_view.moveCursor(QTextCursor.End)

    def _start(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        links = [ln.strip() for ln in self.input.toPlainText().splitlines() if ln.strip()]
        if not links:
            QMessageBox.information(self, "还差点东西", "先把抖音分享链接粘进来（一行一个）。")
            return

        self._save_cfg()
        if self.cfg.get("baidu", {}).get("mode") == "copy" and not self.ed_sync.text().strip():
            if QMessageBox.question(
                self,
                "还没设置网盘同步目录",
                "现在没有配置百度网盘同步文件夹，文件只会存在本地。\n要现在去设置吗？",
            ) == QMessageBox.Yes:
                self._pick_sync()
                if not self.ed_sync.text().strip():
                    return

        headless = not self.cb_show_browser.isChecked()
        self.log_view.clear()
        self.progress.setRange(0, 0)
        self.status.setText("正在启动浏览器…")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_login.setEnabled(False)

        self.worker = Worker(self.cfg, links, headless)
        self.worker.log.connect(self._append_log)
        self.worker.progress.connect(self._on_progress)
        self.worker.ask_dup.connect(self._on_ask_dup)
        self.worker.finished_summary.connect(self._on_done)
        self.worker.start()

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self.status.setText("正在停止…")
            self.btn_stop.setEnabled(False)
        for w in (self.sync_worker, self.reclassify_worker):
            if w and w.isRunning():
                w.stop()
                self.status.setText("正在停止…")
                self.btn_stop.setEnabled(False)

    def _on_ask_dup(self, title: str, author: str, holder: dict, ev) -> None:
        """工作线程碰到已下载过的作品，停下来问一下。"""
        self.status.setText("等你确认要不要重新下载…")
        box = QMessageBox(self)
        box.setWindowTitle("这条已经下过了")
        box.setIcon(QMessageBox.Question)
        box.setText(f"《{title}》")
        box.setInformativeText(
            f"@{author}\n\n本地已经有这个作品了（可能也已经传到网盘）。\n要重新下载、覆盖旧文件吗？"
        )
        b_again = box.addButton("重新下载", QMessageBox.AcceptRole)
        b_skip = box.addButton("跳过这条", QMessageBox.RejectRole)
        b_all = box.addButton("后面全部重新下载", QMessageBox.AcceptRole)
        b_all_skip = box.addButton("后面全部跳过", QMessageBox.RejectRole)
        box.setDefaultButton(b_skip)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_again:
            mode = "overwrite"
        elif clicked is b_all:
            mode = "all_overwrite"
        elif clicked is b_all_skip:
            mode = "all_skip"
        else:
            mode = "skip"
        holder["mode"] = mode
        ev.set()
        self.status.setText("继续下载…")

    def _on_progress(self, done: int, total: int, label: str) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status.setText(f"{done}/{total}  ·  {label[:60]}")

    def _on_done(self, summary: str) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.status.setText(summary)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_login.setEnabled(True)
        self._refresh_login_label()

    def closeEvent(self, event) -> None:  # noqa: N802
        auto = bool((self.cfg.get("auto_sync") or {}).get("enabled"))
        if auto and getattr(self, "tray", None) and not getattr(self, "_force_quit", False):
            event.ignore()
            self.hide()
            if not self._tray_tip_shown:
                self._tray_tip_shown = True
                self._save_cfg()
                self.tray.showMessage(
                    APP_NAME,
                    "已缩到右下角托盘，会按设定时间自动检查收藏。右键托盘图标可以退出。",
                    QSystemTrayIcon.Information,
                    5000,
                )
            return
        if self.login_worker and self.login_worker.isRunning():
            self.login_worker.stop()
            self.login_worker.wait(4000)
        if self.worker and self.worker.isRunning():
            if QMessageBox.question(self, "还在下载", "任务还没结束，确定退出吗？") != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.stop()
            self.worker.wait(4000)
        if self.sync_worker and self.sync_worker.isRunning():
            if QMessageBox.question(self, "正在同步", "同步还没结束，确定退出吗？") != QMessageBox.Yes:
                event.ignore()
                return
            self.sync_worker.stop()
            self.sync_worker.wait(4000)
        if self.reclassify_worker and self.reclassify_worker.isRunning():
            if QMessageBox.question(self, "正在整理", "整理还没结束，确定退出吗？") != QMessageBox.Yes:
                event.ignore()
                return
            self.reclassify_worker.stop()
            self.reclassify_worker.wait(4000)
        if self.upload_worker and self.upload_worker.isRunning():
            self.upload_worker.stop()
            self.upload_worker.wait(3000)
        if getattr(self, "pan_worker", None) and self.pan_worker.isRunning():
            self.pan_worker.stop()
            self.pan_worker.wait(3000)
        self._save_cfg()
        if getattr(self, "tray", None):
            self.tray.hide()
        event.accept()


def _crash_log(text: str) -> None:
    try:
        (cfgmod.ROOT / "启动错误.log").write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def selftest() -> int:
    """打包后自检：确认 Qt / playwright / ffmpeg 都被正确装进来了。"""
    lines = []
    ok = True
    for name in ("PySide6", "playwright", "imageio_ffmpeg", "requests"):
        try:
            mod = __import__(name)
            lines.append(f"OK   {name} {getattr(mod, '__version__', '')}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"FAIL {name}: {exc}")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        lines.append("OK   playwright.sync_api")
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(f"FAIL playwright.sync_api: {exc}")
    for mod in ("core.linkparse", "core.config", "core.douyin", "core.downloader", "core.pipeline", "core.baidu"):
        try:
            __import__(mod)
            lines.append(f"OK   {mod}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"FAIL {mod}: {exc}")
    try:
        from core.downloader import ffmpeg_exe

        lines.append(f"OK   ffmpeg: {ffmpeg_exe()}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(f"FAIL ffmpeg: {exc}")
    lines.append(f"root: {cfgmod.ROOT}")
    text = "\n".join(lines)
    (cfgmod.ROOT / "selftest.log").write_text(text, encoding="utf-8")
    print(text)
    return 0 if ok else 1


def smoke() -> int:
    """诊断用：真起一次窗口，截图后自动退出，全过程写 smoke.log。"""
    log: list[str] = ["start"]
    out = cfgmod.ROOT / "smoke.log"

    def dump() -> None:
        try:
            out.write_text("\n".join(log), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    try:
        log.append(f"frozen={getattr(sys, 'frozen', False)}")
        app = QApplication(sys.argv)
        log.append(f"QApplication ok, platform={app.platformName()}")
        win = MainWindow()
        win.load_from_cfg()
        log.append(f"window built {win.width()}x{win.height()}")
        win.show()
        app.processEvents()
        log.append("shown")

        def snap() -> None:
            try:
                win.grab().save(str(cfgmod.ROOT / "ui_preview.png"))
                log.append("screenshot ok")
            except Exception as exc:  # noqa: BLE001
                log.append(f"screenshot fail: {exc}")
            dump()
            app.quit()

        QTimer.singleShot(2500, snap)
        app.exec()
        log.append("closed")
        dump()
        return 0
    except Exception:  # noqa: BLE001
        import traceback

        log.append(traceback.format_exc())
        dump()
        return 1


def qtdebug() -> int:
    """诊断用：把 Qt 自己的消息（含插件加载失败原因）写进 qt_debug.log。"""
    out = cfgmod.ROOT / "qt_debug.log"
    lines: list[str] = []

    def w(msg: str) -> None:
        lines.append(msg)
        try:
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    try:
        os.environ["QT_DEBUG_PLUGINS"] = "1"
        w("frozen=%s" % getattr(sys, "frozen", False))
        w("exe=%s" % sys.executable)
        w("argv=%s" % sys.argv)

        from PySide6.QtCore import qInstallMessageHandler

        def handler(mode, ctx, msg):  # noqa: ANN001
            w(f"[qt] {msg}")

        qInstallMessageHandler(handler)
        w("handler installed")

        from PySide6.QtWidgets import QApplication

        w("QApplication imported")
        w("QT_PLUGIN_PATH=%s" % os.environ.get("QT_PLUGIN_PATH", ""))
        app = QApplication(sys.argv)
        w(f"QApplication created, platform={app.platformName()}")
        return 0
    except Exception:  # noqa: BLE001
        import traceback

        w(traceback.format_exc())
        return 1


def e2e(url: str) -> int:
    """诊断用：在打包环境里真跑一次完整下载（浏览器 + 下载 + ffmpeg）。"""
    out = cfgmod.ROOT / "e2e.log"
    lines: list[str] = []

    def w(msg: str) -> None:
        lines.append(str(msg))
        try:
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    try:
        w(f"frozen={getattr(sys, 'frozen', False)} url={url}")
        from core.pipeline import Pipeline

        cfg = cfgmod.load()
        cfg["baidu"] = {"enabled": False, "mode": "off"}
        p = Pipeline(cfg=cfg, log=w)
        p.run_link(url, headless=True)
        w("SUMMARY " + p.summary())
        return 0
    except Exception:  # noqa: BLE001
        import traceback

        w(traceback.format_exc())
        return 1


def login_cli() -> int:
    """诊断用：不开界面，直接弹浏览器扫码登录。"""
    out = cfgmod.ROOT / "login.log"
    lines: list[str] = []

    def w(msg: str) -> None:
        lines.append(str(msg))
        try:
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    cfg = cfgmod.load()
    prof = douyin_profile(Path(cfg.get("download_dir") or cfgmod.ROOT))
    w(f"profile: {prof}")
    ok = qr_login(prof, log=w)
    w(f"RESULT {ok}")
    return 0 if ok else 1


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--login" in sys.argv:
        sys.exit(login_cli())
    if "--e2e" in sys.argv:
        i = sys.argv.index("--e2e")
        sys.exit(e2e(sys.argv[i + 1] if len(sys.argv) > i + 1 else ""))
    if "--qtdebug" in sys.argv:
        sys.exit(qtdebug())
    if "--smoke" in sys.argv:
        sys.exit(smoke())
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    ico = ensure_icon_file()
    if ico:
        app.setWindowIcon(QIcon(str(ico)))
    win = MainWindow()
    win.load_from_cfg()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback

        _crash_log(traceback.format_exc())
        raise
