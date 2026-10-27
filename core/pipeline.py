"""一条龙的下载 + 存网盘流程。

单个链接 → 解析 → 拿无水印地址 → 下载（视频 / 图集 / 背景音乐）→ 传百度网盘

目录约定（一个作品一个文件夹）：
    下载目录/<抖音收藏夹>/<作者>/<日期_标题_ID>/xxx.mp4
    下载目录/<作者>/<日期_标题_ID>/xxx.mp4          （单条链接、不分类时）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import downloader as dl
from .baidu import BaiduPan, pan_safe_path
from .config import load as load_config
from .douyin import DouyinBrowser, FetchError, profile_dir
from .linkparse import classify, extract_url

STATE_NAME = ".downloaded.json"
COLLECT_MAP_NAME = ".collect_map.json"      # aweme_id -> 收藏夹名 的缓存
WORK_SUFFIX = re.compile(r"_\d{6}$")     # 作品文件夹名结尾的 6 位 aweme_id


def _state_path(root: Path) -> Path:
    return root / STATE_NAME


def _load_state(root: Path) -> set[str]:
    p = _state_path(root)
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            return set()
    return set()


def _save_state(root: Path, done: set[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _state_path(root).write_text(
        json.dumps(sorted(done), ensure_ascii=False, indent=0), encoding="utf-8"
    )


def is_work_dir(p: Path) -> bool:
    return p.is_dir() and bool(WORK_SUFFIX.search(p.name))


def digged_only(awemes: list[dict]) -> list[dict]:
    """「收藏 + 点赞」的交集筛选：只留既在收藏夹、又点过赞的作品。

    抖音的收藏列表接口本身就带 user_digged（1 = 我点过赞），不用再多发请求。
    """
    return [a for a in awemes if int(a.get("user_digged") or 0) == 1]


def collect_folder_names(cfg: dict, root: Path | str) -> set[str]:
    """收藏板块会用到的顶层文件夹名：抖音收藏夹名 + 兜底目录（视频）。

    收藏板块的目录是「收藏夹/作品」，下载板块是「作者/作品」，两者层级一样，
    所以补传时只能靠名字区分。
    """
    org = cfg.get("organize") or {}
    names = {str(org.get("fallback_folder") or "视频")}
    p = Path(root) / COLLECT_MAP_NAME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for f in data.get("folders") or []:
            n = str(f.get("name") or "").strip()
            if n:
                names.add(n)
    except Exception:  # noqa: BLE001
        pass
    return {dl.safe_name(n, 40) for n in names}


def pan_remote_dir(cfg: dict, kind: str = "collect") -> str:
    """两个板块各自的网盘目录名，最终路径是「我的应用数据 / <这个名字>」。

    collect = 收藏自动备份（收藏 + 点赞）→ 抖音收藏
    link    = 分享链接下载              → 视频备份
    """
    b = cfg.get("baidu") or {}
    if kind == "link":
        val = b.get("remote_dir_link") or "视频备份"
    else:
        val = b.get("remote_dir_collect")
        if not val:
            # 老配置只有 remote_dir：用户自己改过就沿用，还是默认的「抖音备份」就用新默认值
            old = str(b.get("remote_dir") or "").strip()
            val = old if old and old != "抖音备份" else "抖音收藏"
    return str(val).strip().strip("/") or ("视频备份" if kind == "link" else "抖音收藏")


class Pipeline:
    def __init__(
        self,
        cfg: dict | None = None,
        log=print,
        on_progress=None,
        stop=None,
        ask_duplicate=None,
        upload_kind: str = "collect",
    ):
        self.cfg = cfg or load_config()
        # 上传到哪个网盘目录：collect = 抖音收藏；link = 视频备份
        self.upload_kind = "link" if str(upload_kind).lower() == "link" else "collect"
        self.log = log
        self.on_progress = on_progress or (lambda done, total, label: None)
        self._stop = stop or (lambda: False)
        # 已下载过的作品怎么处理：skip / overwrite / ask
        self.dup_mode = str(self.cfg.get("duplicate", "skip") or "skip")
        # ask 模式下由界面回调决定，返回 "overwrite" 或 "skip"
        self.ask_duplicate = ask_duplicate
        self._skip_hint = False
        self.root = Path(self.cfg["download_dir"])
        self.root.mkdir(parents=True, exist_ok=True)
        org = self.cfg.get("organize") or {}
        # 是否按抖音收藏夹分文件夹；关掉就还是「作者/作品」
        self.by_folder = bool(org.get("by_collect_folder", True))
        # 收藏板块不再套作者层：直接 <收藏夹>/<作品>
        self.flat_folder = self.by_folder and bool(org.get("flat_in_folder", True))
        self.fallback_folder = str(org.get("fallback_folder", "视频") or "视频")
        # 只有收藏板块用「收藏夹/作品」这种平铺结构；下载板块仍是 作者/作品
        self.flat = self.flat_folder and self.upload_kind == "collect"
        b = self.cfg.get("baidu") or {}
        self.pan = BaiduPan(
            mode=b.get("mode", "copy"),
            sync_dir=b.get("sync_dir", ""),
            access_token=b.get("access_token", ""),
            remote_dir=pan_remote_dir(self.cfg, self.upload_kind),
            log=log,
            app_name=b.get("app_name", ""),
            app_key=b.get("app_key", ""),
            secret_key=b.get("secret_key", ""),
            refresh_token=b.get("refresh_token", ""),
            expires_at=b.get("expires_at", 0),
            redirect_uri=b.get("redirect_uri", ""),
            on_token=self._save_pan_token,
        )
        self.upload = bool(b.get("enabled", True)) and b.get("mode") != "off"
        if self.upload and b.get("mode") == "copy" and not b.get("sync_dir"):
            self.log("提示：还没设置百度网盘同步文件夹，本次只存本地")
            self.upload = False
        if self.upload and b.get("mode") == "api":
            if not b.get("access_token"):
                self.log("提示：还没授权百度网盘，本次只存本地（点界面「网盘」里的授权）")
                self.upload = False
            # 应用名不再是必需的：不填就传到「我的应用数据 / 网盘目录」这一层
        self.done = _load_state(self.root)
        self.stats = {"video": 0, "image": 0, "failed": 0, "skip": 0, "redo": 0, "moved": 0}
        self._index: dict[str, Path] = {}
        self._build_index()
        self._migrate_layout()

    # ---------- 目录扫描 ----------
    def _iter_work_dirs(self):
        """遍历所有作品文件夹，产出 (作品目录, 是否已按收藏夹分层)。"""
        try:
            top = [d for d in self.root.iterdir() if d.is_dir() and not d.name.startswith(".")]
        except OSError:
            return
        for d1 in top:
            try:
                subs = [d for d in d1.iterdir() if d.is_dir()]
            except OSError:
                continue
            for d2 in subs:
                if is_work_dir(d2):
                    yield d2, False          # 下载目录/作者/作品
                    continue
                try:
                    inner = [d for d in d2.iterdir() if d.is_dir()]
                except OSError:
                    continue
                for d3 in inner:
                    if is_work_dir(d3):
                        yield d3, True       # 下载目录/收藏夹/作者/作品

    def _iter_author_dirs(self):
        """作者目录：下载目录/作者 以及 下载目录/收藏夹/作者。"""
        try:
            top = [d for d in self.root.iterdir() if d.is_dir() and not d.name.startswith(".")]
        except OSError:
            return
        for d1 in top:
            if is_work_dir(d1):
                continue
            try:
                subs = [d for d in d1.iterdir() if d.is_dir()]
            except OSError:
                continue
            if any(is_work_dir(s) for s in subs):
                yield d1                    # 下载目录/作者
                continue
            for d2 in subs:                 # 下载目录/收藏夹/作者
                if is_work_dir(d2):
                    continue
                try:
                    inner = [x for x in d2.iterdir() if x.is_dir()]
                except OSError:
                    continue
                if any(is_work_dir(x) for x in inner):
                    yield d2

    def _build_index(self) -> None:
        """作品文件夹名 -> 路径。作品名带 aweme_id 后 6 位，全库唯一，用来跨收藏夹判断下没下过。"""
        idx: dict[str, Path] = {}
        for w, _ in self._iter_work_dirs():
            idx.setdefault(w.name, w)
        self._index = idx

    # ---------- 旧目录结构整理 ----------
    def _migrate_layout(self) -> None:
        """老版本把 mp4/mp3 直接丢在作者目录下，现在统一「一个作品一个文件夹」，
        启动时顺手收拾一次（纯移动，不会删内容）。"""
        moved = 0
        for ad in self._iter_author_dirs():
            try:
                entries = list(ad.iterdir())
            except OSError:
                continue
            for f in entries:
                if not f.is_file() or f.suffix.lower() not in (".mp4", ".mp3", ".m4a"):
                    continue
                tdir = ad / f.stem
                try:
                    tdir.mkdir(parents=True, exist_ok=True)
                    target = tdir / f.name
                    if target.exists():
                        # 作品文件夹里已经有了，保留大的那个
                        if target.stat().st_size >= f.stat().st_size:
                            f.unlink(missing_ok=True)
                            continue
                        target.unlink(missing_ok=True)
                    f.replace(target)
                    moved += 1
                except OSError as exc:
                    self.log(f"  整理旧文件失败：{f.name}（{exc}）")
        if moved:
            self.log(f"已把 {moved} 个旧文件收进对应的「作品文件夹」")
            self._build_index()

    # ---------- 浏览器 Profile ----------
    def _profile(self) -> Path:
        """登录态默认放程序目录；配置里指定 profile_dir 时优先用它（测试/多账号用）。"""
        custom = str(self.cfg.get("profile_dir") or "").strip()
        return Path(custom) if custom else profile_dir(self.root)

    def _save_pan_token(self, tok: dict) -> None:
        """token 续期后写回 config.json，下次启动还能用。"""
        b = dict(self.cfg.get("baidu") or {})
        b.update(tok)
        self.cfg["baidu"] = b
        try:
            from .config import save as save_cfg

            self.cfg = save_cfg(self.cfg)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 上传 ----------
    def _push(self, local: Path, subdir: str) -> str:
        if not self.upload:
            return str(local)
        try:
            return self.pan.save(local, subdir)
        except Exception as exc:  # noqa: BLE001
            self.log(f"  ! 网盘上传失败，文件保留在本地：{local}（{exc}）")
            return str(local)

    # ---------- 路径 ----------
    def _base_dir(self, folder: str | None = None) -> Path:
        """收藏板块落盘的顶层目录：有收藏夹就进收藏夹，没有（只点赞 / 未归夹）就进「视频」。"""
        return self.root / dl.safe_name(folder or self.fallback_folder, 40)

    def _folder_dir(self, folder: str | None) -> Path:
        # 关掉「按收藏夹分文件夹」时，folder 参数直接不管
        if folder and self.by_folder:
            return self.root / dl.safe_name(folder, 40)
        return self.root

    def _author_dir(self, meta: dict, folder: str | None = None) -> Path:
        return self._folder_dir(folder) / dl.safe_name(meta["author"], 40)

    def _work_dir(self, meta: dict, folder: str | None = None) -> Path:
        """一个作品一个文件夹。

        收藏板块：下载目录/收藏夹/作品/        （不再套作者层）
        下载板块：下载目录/作者/作品/          （分享链接、博主主页）
        """
        if self.flat:
            return self._base_dir(folder) / meta["title"]
        return self._author_dir(meta, folder) / meta["title"]

    def _subdir(self, meta: dict, folder: str | None = None) -> str:
        """网盘里的相对子目录：收藏板块 <收藏夹>/<作品>，下载板块 <作者>/<作品>。"""
        if self.flat:
            return f"{self._base_dir(folder).name}/{meta['title']}"
        bits = []
        if folder and self.by_folder:
            bits.append(dl.safe_name(folder, 40))
        bits += [dl.safe_name(meta["author"], 40), meta["title"]]
        return "/".join(bits)

    def _net_dir(self, meta: dict, folder: str | None = None) -> Path | None:
        """「丢进同步文件夹」模式下，文件会被 move 到网盘同步目录，那边也算已下载。"""
        if not self.upload or self.pan.mode != "copy" or not self.pan.sync_dir:
            return None
        base = Path(self.pan.sync_dir)
        if self.flat:
            return base / self._base_dir(folder).name / meta["title"]
        if folder and self.by_folder:
            base = base / dl.safe_name(folder, 40)
        return base / dl.safe_name(meta["author"], 40) / meta["title"]

    def _legacy_dirs(self, meta: dict):
        """这个作品在旧目录结构里可能待的地方（作者/作品、收藏夹/作者/作品）。"""
        author = dl.safe_name(meta["author"], 40)
        yield self.root / author / meta["title"]
        try:
            tops = [d for d in self.root.iterdir() if d.is_dir() and not d.name.startswith(".")]
        except OSError:
            return
        for d in tops:
            yield d / author / meta["title"]

    @staticmethod
    def _drop_empty(folder: Path) -> None:
        """空的作品文件夹不留。"""
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass

    # ---------- 是否已下载 ----------
    @staticmethod
    def _has_media(d: Path, title: str) -> bool:
        if not d.is_dir():
            return False
        out = d / f"{title}.mp4"
        if out.exists() and out.stat().st_size > 0:
            return True
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            for p in d.glob(pattern):
                if p.stat().st_size > 0:
                    return True
        for name in (f"{title}.mp3", f"{title}.m4a", "背景音乐.mp3", "背景音乐.m4a"):
            f = d / name
            if f.exists() and f.stat().st_size > 0:
                return True
        return False

    def _locate(self, meta: dict, folder: str | None = None) -> Path | None:
        """这个作品已经下到哪了（不管在哪个收藏夹下）。"""
        cand = self._index.get(meta["title"])
        if cand and self._has_media(cand, meta["title"]):
            return cand
        tries = [
            self._work_dir(meta, folder),
            self.root / dl.safe_name(self.fallback_folder, 40) / meta["title"],
            self._author_dir(meta, folder) / meta["title"],
        ]
        tries += list(self._legacy_dirs(meta))
        for d in tries:
            if self._has_media(d, meta["title"]):
                return d
        net = self._net_dir(meta, folder)
        if net and self._has_media(net, meta["title"]):
            return net
        return None

    def _should_relocate(self, folder: str | None) -> bool:
        """要不要把已经下好的作品挪到目标位置。

        下载板块（作者/作品）看到文件已经存在就别动它 —— 它可能正待在某个收藏夹里，
        挪到作者目录反而是错的。收藏板块则总是按收藏夹归位。
        """
        return self.flat or bool(folder)

    def _move_work_dir(self, src: Path, meta: dict, folder: str | None) -> Path | None:
        """作品已在旧位置（比如还没分收藏夹），直接挪到目标位置，不重新下载。"""
        dest = self._work_dir(meta, folder)
        if src.resolve() == dest.resolve() or not src.is_dir():
            return src if src.is_dir() else None
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for f in list(src.iterdir()):
                target = dest / f.name
                if target.exists():
                    if target.stat().st_size >= f.stat().st_size:
                        f.unlink(missing_ok=True)
                        continue
                    target.unlink(missing_ok=True)
                f.replace(target)
            self._drop_empty(src)
            if src.parent != self.root:
                self._drop_empty(src.parent)      # 老结构搬完只剩空作者目录，顺手清掉
            self._index[dest.name] = dest
            self.stats["moved"] += 1
            self.log(f"  已归位到「{dest.parent.name}」：{dest.name}")
            return dest
        except OSError as exc:
            self.log(f"  归位失败（保留原处）：{src}（{exc}）")
            return src

    # ---------- 重复处理 ----------
    def _decide_duplicate(self, meta: dict) -> str:
        """已下过的作品：返回 skip 或 overwrite。"""
        mode = self.dup_mode
        if mode == "ask" and self.ask_duplicate:
            try:
                mode = str(self.ask_duplicate(meta) or "skip")
            except Exception:  # noqa: BLE001
                mode = "skip"
        return "overwrite" if mode == "overwrite" else "skip"

    def _stale_output(self, meta: dict, aweme: dict, folder: str | None = None) -> bool:
        """已有产物要不要重做：缺音轨（合成时没带音乐）或比音乐短（旧版被截断）。"""
        if meta.get("kind") != "images":
            return False
        if not (self.cfg.get("images") or {}).get("make_video", True):
            return False
        out = self._work_dir(meta, folder) / f"{meta['title']}.mp4"
        if not (out.exists() and out.stat().st_size > 0):
            found = self._locate(meta, folder)
            if not found:
                return False
            out = found / f"{meta['title']}.mp4"
        if not (out.exists() and out.stat().st_size > 0):
            return False
        got, has_audio = dl.probe_media(out)
        if not got:
            return False
        # 作品本身有背景音乐（含页面流兜底），产物却一条音轨都没有 → 必须重做
        if (dl.pick_music(aweme) or aweme.get("_fallback_music")) and not has_audio:
            return True
        bgm = None
        for d in (out.parent, out.parent.parent):
            for name in ("背景音乐.mp3", "背景音乐.m4a", f"{meta['title']}.mp3", f"{meta['title']}.m4a"):
                cand = d / name
                if cand.exists():
                    bgm = cand
                    break
            if bgm:
                break
        want = dl.probe_duration(bgm) if bgm else None
        return bool(want and got < want * 0.9)

    # ---------- 单条作品 ----------
    def _pull(self, urls: list[str], dest: Path, label: str = "地址失效，换一个") -> bool:
        """按顺序试几个地址，成功一个就算成。"""
        for u in (urls or [])[:4]:
            try:
                dl.download_file(u, dest, log=self.log)
                return True
            except Exception as exc:  # noqa: BLE001
                self.log(f"  {label}：{exc}")
        return False

    def _refresh_once(self, aid: str, refresh, tried: list) -> dict | None:
        """地址过期时重新抓一次作品详情，拿新的签名地址。

        抖音的 play 地址带时效签名，收藏夹翻得多、跑得久就会 403/404，
        换 url_list 里别的地址也没用（同一个签名）。这里只重抓一次，
        避免每条都开浏览器页面被风控。
        """
        if not refresh or tried[0] or not aid:
            return None
        tried[0] = True
        self.log("  地址已过期，重新取一次作品详情…")
        try:
            fresh = refresh(aid)
        except Exception as exc:  # noqa: BLE001
            self.log(f"  重新取详情失败：{exc}")
            return None
        if not fresh:
            self.log("  重新取详情没拿到数据")
        return fresh or None

    def save_aweme(
        self,
        aweme: dict,
        force: bool = False,
        folder: str | None = None,
        refresh=None,
    ) -> bool:
        """保存一条作品。folder = 它所在的抖音收藏夹（决定放在哪个子目录）。

        refresh: 可选，传 aweme_id -> 详情 dict 的函数（通常是 br.fetch_aweme）。
        地址过期（403/404）时用它重抓一次，换新签名地址再下。
        """
        meta = dl.aweme_meta(aweme)
        aid, title = meta["aweme_id"], meta["title"]
        if not aid:
            return False
        if folder and not self.by_folder:
            folder = None
        tried_refresh = [False]        # 一条作品最多重抓一次详情，别把风控惹毛

        stale = self._stale_output(meta, aweme, folder)
        redo = force
        if stale:
            self.log(f"  已有视频不对（缺音乐或比音乐短），重新合成：{title}")
            redo = True
        elif not redo:
            where = self._locate(meta, folder)
            if aid in self.done:
                if not where:
                    self.log(f"  记录里下过但文件不在了，重新下载：{title}")
                    redo = True
                elif self._decide_duplicate(meta) == "overwrite":
                    self.log(f"  重新下载（覆盖旧的）：{title}")
                    redo = True
                    self.stats["redo"] += 1
                else:
                    # 文件在别的收藏夹下面 → 按现在的分类挪过去
                    target = self._work_dir(meta, folder)
                    if self._should_relocate(folder) and where.resolve() != target.resolve():
                        self._move_work_dir(where, meta, folder)
                    self.log(f"  跳过（已下过）：{title}")
                    if not self._skip_hint:
                        self._skip_hint = True
                        self.log("  （不想跳过的话，把「已下载过的」改成「重新下载并覆盖」或「每条问我」）")
                    self.stats["skip"] += 1
                    return True
            elif where:
                # 没记录但文件其实在（上次中断过 / 从别处拷来的）→ 归位就行，不重下
                target = self._work_dir(meta, folder)
                if self._should_relocate(folder) and where.resolve() != target.resolve():
                    self._move_work_dir(where, meta, folder)
                    self.log(f"  本地已有，按收藏夹归位：{title}")
                else:
                    self.log(f"  本地已有，跳过：{title}")
                self.done.add(aid)
                _save_state(self.root, self.done)
                self.stats["skip"] += 1
                return True

        img_cfg = self.cfg.get("images") or {}
        if meta["kind"] != "video" and not (
            img_cfg.get("save_images", True) or img_cfg.get("make_video", True) or img_cfg.get("save_bgm", True)
        ):
            self.log(f"  三个下载选项都没勾，这条什么都不保存：{title}")
            self.stats["failed"] += 1
            return False

        work_dir = self._work_dir(meta, folder)      # 一个作品一个文件夹，勾了什么就放什么
        work_dir.mkdir(parents=True, exist_ok=True)
        sub = self._subdir(meta, folder)             # 网盘里对应的子目录
        got_any = False

        if meta["kind"] == "video":
            urls = dl.pick_video_urls(aweme, prefer=str(self.cfg.get("video_codec", "h264")))
            if not urls:
                self.log(f"  跳过（拿不到无水印地址）：{title}")
                self.stats["failed"] += 1
                self._drop_empty(work_dir)
                return False
            dest = work_dir / f"{title}.mp4"
            if redo:
                dest.unlink(missing_ok=True)
            if dest.exists() and dest.stat().st_size > 0:
                got_any = True
            else:
                got_any = self._pull(urls, dest)
                if not got_any:
                    fresh = self._refresh_once(aid, refresh, tried_refresh)
                    if fresh:
                        aweme = fresh
                        got_any = self._pull(
                            dl.pick_video_urls(aweme, prefer=str(self.cfg.get("video_codec", "h264"))),
                            dest,
                            label="新地址还是不行，换一个",
                        )
                if not got_any:
                    self.log(f"  × 视频下载失败（地址全失效）：{title}")
            if got_any:
                self.log(f"  ✓ 视频 {dest.name}")
                self._push(dest, sub)
                self.stats["video"] += 1
            if got_any and img_cfg.get("save_bgm", True):
                self._save_music(
                    aweme, work_dir, title, subdir=sub, refresh=refresh, tried=tried_refresh
                )

        else:  # 图文
            notes = dl.pick_images(aweme)
            lives = dl.pick_live_clip_urls(aweme)
            if not notes:
                self.log(f"  跳过（图集为空）：{title}")
                self.stats["failed"] += 1
                return False
            imgs: list[Path] = []
            clips: list[Path | None] = []      # 和 imgs 对齐：实况位放它的小视频
            for i, u in enumerate(notes, 1):
                urls = lives[i - 1] if i - 1 < len(lives) else []
                if urls:
                    # 实况/动态图：下它的小视频，合成时按自然时长插进片子里
                    lp = work_dir / f"{i:02d}_动态.mp4"
                    if not (lp.exists() and lp.stat().st_size > 0 and not redo):
                        ok = self._pull(urls, lp, label=f"第 {i} 张实况视频换源重试")
                        if not ok:
                            fresh = self._refresh_once(aid, refresh, tried_refresh)
                            if fresh:
                                aweme = fresh
                                l2 = dl.pick_live_clip_urls(aweme)
                                if i - 1 < len(l2):
                                    ok = self._pull(l2[i - 1], lp, label=f"第 {i} 张实况视频新地址")
                        if not ok:
                            lp = None
                    if lp is not None and lp.exists() and lp.stat().st_size > 0:
                        imgs.append(lp)
                        clips.append(lp)
                        continue
                    self.log(f"  第 {i} 张实况视频拿不到，退回静态图")
                clips.append(None)
                p = work_dir / f"{i:02d}{dl.img_suffix(u)}"
                if p.exists() and p.stat().st_size > 0 and not redo:
                    imgs.append(p)
                    continue
                ok = self._pull([u], p, label=f"第 {i} 张图换源重试")
                if not ok:
                    fresh = self._refresh_once(aid, refresh, tried_refresh)
                    if fresh:
                        aweme = fresh
                        n2 = dl.pick_images(aweme)
                        if i - 1 < len(n2):
                            u = n2[i - 1]
                            p = work_dir / f"{i:02d}{dl.img_suffix(u)}"
                            ok = self._pull([u], p, label=f"第 {i} 张图新地址")
                if ok:
                    imgs.append(p)
                else:
                    self.log(f"  第 {i} 张图失败（地址失效）")
            if not imgs:
                self.stats["failed"] += 1
                return False
            n_live = sum(1 for c in clips if c is not None)

            keep_images = bool(img_cfg.get("save_images", True))
            want_video = bool(img_cfg.get("make_video", True))
            want_mp3 = bool(img_cfg.get("save_bgm", True))

            if keep_images:
                for p in imgs:
                    self.log(f"  ✓ 图片 {p.name}（共 {len(imgs)} 张）")
                    self._push(p, sub)
                self.stats["image"] += 1
                got_any = True

            # 合成视频必须有音乐；「另存背景音乐」只决定要不要单独留一份音频
            bgm = None
            if want_video or want_mp3:
                bgm = self._save_music(
                    aweme, work_dir, "背景音乐", subdir=sub, push=want_mp3,
                    refresh=refresh, tried=tried_refresh,
                )

            if want_video:
                out = work_dir / f"{title}.mp4"
                if out.exists() and out.stat().st_size > 0 and not stale and not redo:
                    got_any = True
                else:
                    if stale or redo:
                        out.unlink(missing_ok=True)
                    self.log(
                        f"  合成图文视频（{len(imgs)} 张 + 背景音乐"
                        + (f"，{n_live} 段实况动图" if n_live else "") + "）…"
                    )
                    per = float(img_cfg.get("image_seconds", 2.6) or 2.6)
                    if dl.make_slideshow(imgs, bgm, out, per_image=per, log=self.log, clips=clips):
                        self.log(f"  ✓ 图文视频 {out.name}")
                        self._push(out, sub)
                        got_any = True

            # 没勾的东西不留在文件夹里
            if not keep_images:
                for p in imgs:
                    p.unlink(missing_ok=True)
            if not want_mp3 and bgm and bgm.exists():
                bgm.unlink(missing_ok=True)

        # 空文件夹不留下
        self._drop_empty(work_dir)

        if got_any:
            self.done.add(aid)
            _save_state(self.root, self.done)
            self._index[title] = work_dir
        else:
            self.stats["failed"] += 1
        return got_any

    def _save_music(
        self,
        aweme: dict,
        folder: Path,
        name: str,
        subdir: str,
        push: bool = False,
        refresh=None,
        tried: list | None = None,
    ) -> Path | None:
        picked = dl.pick_music_detail(aweme)
        if not picked:
            self.log("  ! 这个作品的音乐地址拿不到（可能是版权音乐），合成视频将没有声音")
            return None
        url, suffix = picked
        for alt in (suffix, ".mp3", ".m4a"):
            have = folder / f"{name}{alt}"
            if have.exists() and have.stat().st_size > 0:
                return have
        dest = folder / f"{name}{suffix}"
        if not self._pull([url], dest, label="背景音乐地址失效"):
            # 音频签名地址也会过期，重抓一次详情再试
            fresh = self._refresh_once(str(aweme.get("aweme_id") or ""), refresh, tried or [False])
            again = dl.pick_music_detail(fresh) if fresh else None
            if not again:
                self.log("  背景音乐下载失败，合成视频将没有声音")
                return None
            url, suffix = again
            dest = folder / f"{name}{suffix}"
            if not self._pull([url], dest, label="背景音乐新地址也不行"):
                return None
        real = dl.detect_audio_suffix(dest)
        if real != suffix:
            fixed = dest.with_suffix(real)
            try:
                dest.replace(fixed)
                dest = fixed
            except Exception:  # noqa: BLE001
                pass
        if push:
            self._push(dest, subdir)
        return dest

    def _load_collect_map(self, max_age_days: float = 30) -> dict[str, str]:
        """aweme_id -> 收藏夹名 的缓存（由「整理已有备份」或全量同步写出）。"""
        p = self.root / COLLECT_MAP_NAME
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        if max_age_days and time.time() - float(data.get("ts") or 0) > max_age_days * 86400:
            return {}
        return dict(data.get("map") or {})

    def _pan_tail_set(self) -> set[str]:
        """网盘上已有的作品 ID 尾号集合（<收藏夹>/<作品>/ 两层目录）。

        对账补漏用：账本说备份过、但网盘上没有的，就算漏了。
        """
        tails: set[str] = set()
        if not (self.upload and self.pan.mode == "api"):
            return tails
        try:
            root = self.pan.remote_root()
            for d in self.pan.list_dir(root):
                if not d["isdir"]:
                    continue
                for w in self.pan.list_dir(d["path"]):
                    if w["isdir"] and WORK_SUFFIX.search(w["name"]):
                        tails.add(w["name"][-6:])
        except Exception as exc:  # noqa: BLE001
            self.log(f"  读网盘目录失败（本次对账只看本地）：{exc}")
        return tails

    def _folder_title_subdir(self, title: str, folder: str | None) -> str:
        """收藏板块里某个作品在网盘上的子目录：<收藏夹>/<作品>。"""
        if self.flat:
            return f"{self._base_dir(folder).name}/{title}"
        return f"{dl.safe_name(folder or self.fallback_folder, 40)}/{title}"

    def _ensure_on_pan(self, work_dir: Path, folder: str | None, force: bool = False) -> int:
        """网盘上缺哪个文件就补哪个（对账补漏）。force=True 时全部重传。"""
        if not self.upload or not work_dir.is_dir():
            return 0
        sub = self._folder_title_subdir(work_dir.name, folder)
        have: set[str] = set()
        if not force:
            try:
                have = {
                    d["name"] for d in self.pan.list_dir(f"{self.pan.remote_root()}/{sub}")
                }
            except Exception:  # noqa: BLE001
                have = set()
        n = 0
        for f in sorted(work_dir.iterdir()):
            # 本地文件名可能带 emoji，网盘上是净化过的名字，比对时要做同样处理
            if f.is_file() and not f.name.endswith(".part") and pan_safe_path(f.name) not in have:
                self._push(f, sub)
                n += 1
        return n

    def _merge_collect_cache(self, folders: list[dict], id2folder: dict[str, str]) -> None:
        """把这次扫到的收藏夹名单和作品归属合并进缓存（不覆盖以前扫到的老作品）。"""
        if not folders and not id2folder:
            return
        p = self.root / COLLECT_MAP_NAME
        old: dict[str, str] = {}
        old_folders: list[dict] = []
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                old = dict(data.get("map") or {})
                old_folders = list(data.get("folders") or [])
            except Exception:  # noqa: BLE001
                pass
        merged = dict(old)
        merged.update(id2folder or {})
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(
                    {"ts": time.time(), "folders": folders or old_folders, "map": merged},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    # ---------- 我的收藏夹自动同步 ----------
    def sync_favorites(
        self,
        headless: bool = True,
        scope: str = "collect",
        full: bool = False,
        like_limit: int = 1000,
        reconcile: bool = False,
    ) -> dict:
        """按抖音的收藏夹备份「我的收藏」，一个收藏夹一个文件夹。

        保存位置：有收藏夹的进对应收藏夹（<收藏夹>/<作品>），
        没归到任何收藏夹的统一进「视频」文件夹（<视频>/<作品>），不再套作者层。

        scope:
          collect = 只备份收藏（按收藏夹分类）
          like    = 只备份点赞（有收藏夹的进收藏夹，其余进「视频」）
          both    = 收藏 + 点赞，**交集**：只备份既在收藏夹里、又点过赞的作品
        full: 是否逐个收藏夹翻到底（默认只取每个收藏夹最新一页，够覆盖新收藏）
        返回 {"new": 新增, "total": 候选总数, "skipped": 已下过, "folders": 收藏夹数,
              "filtered": 只收藏没点赞被过滤掉的条数}
        """
        scope = (scope or "collect").lower()
        if reconcile:
            full = True      # 对账必须把每个收藏夹翻到底，不然发现不了漏的老作品
        got = {
            "new": 0, "total": 0, "skipped": 0, "folders": 0,
            "error": "", "filtered": 0, "loose": 0, "repaired": 0,
        }
        prev_dup = self.dup_mode
        self.dup_mode = "skip"      # 自动同步不重复下载
        seen_ids: set[str] = set()
        batch = {"total": 0, "filtered": 0}

        pan_tails: set[str] = set()
        if reconcile:
            self.log("对账补漏：先读一遍网盘上已有的作品…")
            pan_tails = self._pan_tail_set()
            self.log(f"网盘上已有 {len(pan_tails)} 个作品")
            if not pan_tails:
                self.log("（网盘上还没扫到作品目录，这次会把范围内全部重新备份）")

        try:
            with DouyinBrowser(self._profile(), headless=headless, log=self.log) as br:
                if scope in ("collect", "both"):
                    def on_items(folder: dict, awemes: list[dict]) -> None:
                        name = folder["name"]
                        fresh = []
                        for a in awemes:
                            aid = str(a.get("aweme_id") or "")
                            if not aid or aid in seen_ids:
                                continue
                            seen_ids.add(aid)
                            fresh.append(a)
                        # 「收藏 + 点赞」= 交集：收藏列表里自带 user_digged，
                        # 只留既在收藏夹、又点过赞的；只收藏没点赞的不备份
                        if scope == "both":
                            liked = digged_only(fresh)
                            dropped = len(fresh) - len(liked)
                            if dropped:
                                batch["filtered"] += dropped
                                got["filtered"] += dropped
                            fresh = liked
                        batch["total"] += len(fresh)
                        if not fresh:
                            return
                        new_ids: list[dict] = []
                        for a in fresh:
                            aid = str(a.get("aweme_id") or "")
                            if reconcile and aid in pan_tails:
                                got["skipped"] += 1        # 网盘上已经有了
                                continue
                            if aid not in self.done:
                                new_ids.append(a)
                                continue
                            if not reconcile:
                                got["skipped"] += 1
                                continue
                            # 账本说备份过、网盘上却没有 → 补漏
                            meta = dl.aweme_meta(a)
                            where = self._locate(meta, name)
                            if where:
                                n = self._ensure_on_pan(where, name)
                                if n:
                                    got["repaired"] += 1
                                    self.log(f"  网盘缺「{meta['title'][:32]}」，补传 {n} 个文件")
                                else:
                                    got["skipped"] += 1
                            else:
                                new_ids.append(a)          # 本地也没有，重下
                        if not new_ids:
                            if got["repaired"] or not reconcile:
                                self.log(f"「{name}」：{len(fresh)} 条都是备份过的")
                            else:
                                self.log(f"「{name}」：{len(fresh)} 条都在网盘上，无缺漏")
                            return
                        self.log(f"「{name}」：{len(fresh)} 条里 {len(new_ids)} 条要补，开始处理")
                        for a in new_ids:
                            if self._stop():
                                break
                            meta = dl.aweme_meta(a)
                            self.on_progress(
                                got["new"], max(batch["total"], got["new"] + 1),
                                f"{name} · {meta['author']} · {dl.safe_name(meta['desc'], 24)}",
                            )
                            try:
                                if self.save_aweme(a, folder=name, refresh=br.fetch_aweme):
                                    got["new"] += 1
                                    if reconcile:
                                        wd = self._work_dir(meta, name)
                                        self._ensure_on_pan(wd, name)
                            except Exception as exc:  # noqa: BLE001
                                self.log(f"  × 失败：{exc}")
                                self.stats["failed"] += 1

                    res = br.collect_folders(
                        full=full, on_items=on_items, stop=self._stop
                    )
                    got["folders"] = len(res.get("folders") or [])
                    got["total"] += batch["total"]
                    if not res.get("folders"):
                        self.log("没读到收藏夹；如果你还没登录，先点「扫码登录」")
                    else:
                        # 收藏夹里没扫到的收藏 = 没归到任何收藏夹的（不进任何收藏夹的那种），
                        # 统一丢进「视频」文件夹。有归属的直接按归属的收藏夹放。
                        self._merge_collect_cache(res.get("folders") or [], res.get("id2folder") or {})
                        known = self._load_collect_map()
                        known.update(res.get("id2folder") or {})
                        got["loose"] += self._save_loose_collects(
                            br, known, seen_ids, got, batch, scope, full
                        )
                if scope == "like" and not self._stop():
                    likes = br.fetch_my_list("like", limit=int(like_limit or 0))
                    fresh = [a for a in likes if str(a.get("aweme_id")) not in seen_ids]
                    known = self._load_collect_map()
                    self.log(f"点赞 {len(likes)} 条，其中 {len(fresh)} 条不在收藏夹里")
                    for a in fresh:
                        if self._stop():
                            break
                        aid = str(a.get("aweme_id"))
                        if aid in self.done:
                            got["skipped"] += 1
                            continue
                        meta = dl.aweme_meta(a)
                        # 这条如果本来就在某个收藏夹里，就归到那个收藏夹；否则进「视频」
                        folder = known.get(aid)
                        where = folder or self.fallback_folder
                        self.on_progress(got["new"], max(len(fresh), 1), f"{where} · {meta['author']}")
                        try:
                            if self.save_aweme(a, folder=folder, refresh=br.fetch_aweme):
                                got["new"] += 1
                                if not folder:
                                    got["loose"] += 1
                        except Exception as exc:  # noqa: BLE001
                            self.log(f"  × 失败：{exc}")
                    got["total"] += len(fresh)
        except FetchError as exc:
            got["error"] = str(exc)
            self.log(f"同步失败：{exc}")
        finally:
            self.dup_mode = prev_dup

        self.on_progress(got["new"], max(got["total"], 1), "同步完成")
        extra = f"，归位 {self.stats.get('moved', 0)} 条" if self.stats.get("moved") else ""
        if got.get("loose"):
            extra += f"，{got['loose']} 条没有收藏夹 → 放进「{self.fallback_folder}」"
        if got.get("repaired"):
            extra += f"，补传漏掉的 {got['repaired']} 条"
        if scope == "both" and got["filtered"]:
            extra += f"，{got['filtered']} 条只收藏没点赞（按范围设置不备份）"
        self.log(
            f"本次新增 {got['new']} 条（跳过已下过 {got['skipped']} 条{extra}），"
            f"共扫 {got['total']} 条 / {got['folders']} 个收藏夹"
        )
        return got

    def _save_loose_collects(
        self, br, known: dict, seen_ids: set, got: dict, batch: dict, scope: str, full: bool
    ) -> int:
        """收藏了、但没归到任何收藏夹的作品 → 统一放进「视频」文件夹。

        收藏列表接口（listcollection）不带收藏夹字段，所以只能这样判断：
        逐个收藏夹扫过的 id 都记在 seen_ids 里，剩下的就是没归夹的。
        有缓存映射时，老作品也能按它原来的收藏夹放回去。
        """
        limit = 0 if full else 60
        try:
            listed = br.fetch_my_list("collect", limit=limit)
        except FetchError as exc:
            self.log(f"  读收藏列表失败，这轮先不处理「{self.fallback_folder}」：{exc}")
            return 0
        rest = [a for a in listed if str(a.get("aweme_id") or "") not in seen_ids]
        if not rest:
            return 0
        if scope == "both":
            liked = digged_only(rest)
            got["filtered"] += len(rest) - len(liked)
            rest = liked
        if not rest:
            return 0

        plan: list[tuple[dict, str | None]] = []
        for a in rest:
            aid = str(a.get("aweme_id") or "")
            if not aid:
                continue
            seen_ids.add(aid)
            if aid in self.done:
                got["skipped"] += 1
                continue
            plan.append((a, known.get(aid)))
        if not plan:
            return 0

        got["total"] += len(plan)
        n_video = sum(1 for _, f in plan if not f)
        tip = f"，其中 {n_video} 条没有收藏夹 → 「{self.fallback_folder}」" if n_video else ""
        self.log(f"另有 {len(plan)} 条收藏不在收藏夹列表里{tip}")

        saved_video = 0
        for a, folder in plan:
            if self._stop():
                break
            meta = dl.aweme_meta(a)
            where = folder or self.fallback_folder
            self.on_progress(
                got["new"], max(batch["total"], 1), f"{where} · {dl.safe_name(meta['desc'], 24)}"
            )
            try:
                if self.save_aweme(a, folder=folder, refresh=br.fetch_aweme):
                    got["new"] += 1
                    if not folder:
                        saved_video += 1
            except Exception as exc:  # noqa: BLE001
                self.log(f"  × 失败：{exc}")
                self.stats["failed"] += 1
        return saved_video

    # ---------- 旧备份按收藏夹归位 ----------
    def reclassify(self, headless: bool = True, limit_folders: int = 0, use_cache: bool = True) -> dict:
        """把已经下载好的作品，按抖音收藏夹挪进对应文件夹（只移动，不重新下载）。

        会先全量扫一遍收藏夹（拿到每个作品属于哪个收藏夹），再搬本地文件；
        网盘同步文件夹 / 开放平台网盘里的文件一起搬。
        抓到的映射会缓存 3 天，重复整理不用再扫一遍。
        """
        res = {"moved": 0, "unknown": 0, "error": "", "remote": 0}
        id2folder: dict[str, str] = {}
        cache = self.root / COLLECT_MAP_NAME

        if use_cache and cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if time.time() - float(data.get("ts") or 0) < 3 * 86400:
                    id2folder = data.get("map") or {}
                    if id2folder:
                        self.log(f"用上次扫到的收藏夹映射（{len(id2folder)} 条作品）")
            except Exception:  # noqa: BLE001
                id2folder = {}

        if not id2folder:
            try:
                with DouyinBrowser(self._profile(), headless=headless, log=self.log) as br:
                    out = br.collect_folders(
                        full=True, stop=self._stop, limit_folders=limit_folders, cache_path=cache
                    )
                    id2folder = out.get("id2folder") or {}
                    folders = out.get("folders") or []
                if not folders:
                    res["error"] = "没读到收藏夹"
                    self.log("没读到收藏夹，先确认已登录、能打开「我的收藏」")
                    return res
                self.log(f"共 {len(folders)} 个收藏夹、{len(id2folder)} 条作品，开始按收藏夹整理本地文件")
            except FetchError as exc:
                res["error"] = str(exc)
                self.log(f"整理失败：{exc}")
                return res

        tail2folder: dict[str, str] = {}
        for aid, name in id2folder.items():
            tail2folder.setdefault(aid[-6:], name)
        folder_names = {dl.safe_name(n, 40) for n in set(tail2folder.values())}
        folder_names.add(dl.safe_name(self.fallback_folder, 40))

        loc = self._reorganize_local(tail2folder)
        res["moved"] += loc["moved"]
        res["unknown"] += loc["unknown"]
        total = loc["total"]

        # 走开放平台直传的，网盘上的目录也按收藏夹挪一遍
        if self.pan.mode == "api" and self.upload:
            try:
                remote_moved = self._reclassify_remote(tail2folder, folder_names)
                res["remote"] = remote_moved
                if remote_moved:
                    self.log(f"网盘上也整理了 {remote_moved} 个作品")
            except Exception as exc:  # noqa: BLE001
                self.log(f"网盘整理跳过：{exc}")

        self._build_index()
        self.on_progress(total, total, "整理完成")
        self.log(
            f"整理完成：移动 {res['moved']} 个作品"
            + (f"，{res['unknown']} 个在收藏夹里找不到（保留原处）" if res["unknown"] else "")
        )
        return res

    def _reorganize_local(self, tail2folder: dict[str, str]) -> dict:
        """把本地的作品按收藏夹拍平成 <收藏夹>/<作品>（只移动，不重新下载）。

        规则：
          - 抖音里归了收藏夹 → 进那个收藏夹
          - 老的「收藏夹/作者/作品」→ 拍平一层
          - 下载板块的「作者/作品」→ 不动
        """
        res = {"moved": 0, "unknown": 0, "total": 0}
        folder_names = {dl.safe_name(n, 40) for n in set(tail2folder.values())}
        folder_names.add(dl.safe_name(self.fallback_folder, 40))
        works = list(self._iter_work_dirs())
        res["total"] = total = len(works)
        for i, (work, deep) in enumerate(works, 1):
            if self._stop():
                self.log("已停止整理")
                break
            tail = work.name.rsplit("_", 1)[-1]
            folder = tail2folder.get(tail)
            parent = work.parent
            if folder:
                target = dl.safe_name(folder, 40)          # 抖音里归了收藏夹 → 进那个收藏夹
            elif deep:
                target = parent.parent.name                # 老的「收藏夹/作者/作品」→ 拍平
            elif parent.name in folder_names:
                target = parent.name                       # 已经在收藏夹（或「视频」）里了
            else:
                res["unknown"] += 1                        # 下载板块的 作者/作品，不动
                continue
            dest = self.root / target / work.name
            if dest.resolve() == work.resolve() or dest.exists():
                continue
            self.on_progress(i, total, f"整理 {work.name[:26]}")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                work.replace(dest)
                self._drop_empty(parent)
                self._drop_empty(parent.parent)
                self._index[dest.name] = dest
                res["moved"] += 1
                self.log(f"  [{i}/{total}] {target} / {work.name}")
            except OSError as exc:
                self.log(f"  移动失败：{work.name}（{exc}）")

        # 网盘同步文件夹里的一起整理
        if self.pan.mode == "copy" and self.pan.sync_dir and Path(self.pan.sync_dir).is_dir():
            sync_root = Path(self.pan.sync_dir)
            moved = 0
            try:
                top = [d for d in sync_root.iterdir() if d.is_dir() and not d.name.startswith(".")]
            except OSError:
                top = []
            for d1 in top:
                try:
                    subs = [d for d in d1.iterdir() if d.is_dir()]
                except OSError:
                    continue
                for d2 in subs:
                    if is_work_dir(d2):
                        work, deep, parent = d2, False, d1
                    else:
                        inner = [x for x in d2.iterdir() if x.is_dir() and is_work_dir(x)]
                        if not inner:
                            continue
                        work, deep, parent = inner[0], True, d2
                    folder = tail2folder.get(work.name.rsplit("_", 1)[-1])
                    target = dl.safe_name(folder, 40) if folder else (parent.parent.name if deep else parent.name)
                    if target not in folder_names:
                        continue
                    dest = sync_root / target / work.name
                    if dest.resolve() == work.resolve() or dest.exists():
                        continue
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        work.replace(dest)
                        self._drop_empty(parent)
                        moved += 1
                    except OSError:
                        continue
            if moved:
                self.log(f"网盘同步文件夹里也整理了 {moved} 个作品")
                res["moved"] += moved
        return res

    def _reclassify_remote(self, tail2folder: dict[str, str], folder_names: set[str]) -> int:
        """网盘上按同一套规则整理：收藏板块拍平成「收藏夹/作品」，下载板块的「作者/作品」不动。"""
        root = self.pan.remote_root()
        self.log(f"整理网盘目录 {root} …")
        moved = 0

        def is_work(name: str) -> bool:
            return bool(WORK_SUFFIX.search(name))

        def dirs(path: str) -> list[dict]:
            return [d for d in self.pan.list_dir(path) if d["isdir"]]

        for d1 in dirs(root):
            base = d1["name"]
            subs = dirs(d1["path"])
            if base in folder_names:
                # 已经是收藏夹目录：把里面的「作者/作品」拍平，作品直接挂到收藏夹下
                for sub in subs:
                    if is_work(sub["name"]):
                        continue
                    for w in dirs(sub["path"]):
                        if not is_work(w["name"]):
                            continue
                        if self.pan.move(w["path"], d1["path"]):
                            moved += 1
                            self.log(f"  网盘：{base}/{sub['name']}/{w['name']} → {base}/")
                continue

            # 不是收藏夹：作者目录（下载板块）或改了名的旧收藏夹
            for sub in subs:
                if is_work(sub["name"]):
                    folder = tail2folder.get(sub["name"].rsplit("_", 1)[-1])
                    if not folder:
                        continue                      # 下载板块的作品，留在作者目录
                    if self.pan.move(sub["path"], f"{root}/{dl.safe_name(folder, 40)}"):
                        moved += 1
                        self.log(f"  网盘：{base}/{sub['name']} → {folder}/")
                    continue
                for w in dirs(sub["path"]):
                    if not is_work(w["name"]):
                        continue
                    folder = tail2folder.get(w["name"].rsplit("_", 1)[-1]) or base
                    if self.pan.move(w["path"], f"{root}/{dl.safe_name(folder, 40)}"):
                        moved += 1
                        self.log(f"  网盘：{base}/{sub['name']}/{w['name']} → {folder}/")
        return moved

    # ---------- 单条链接 ----------
    def run_link(self, text: str, headless: bool | None = None) -> None:
        url = extract_url(text) or (text.strip() if text.strip().startswith("http") else None)
        if not url:
            self.log(f"× 没找到链接：{text[:40]}")
            return
        info = classify(url)
        if headless is None:
            headless = bool(self.cfg.get("headless", True))

        with DouyinBrowser(self._profile(), headless=headless, log=self.log) as br:
            if info["kind"] == "short":
                real = br.resolve_short(url)
                self.log(f"短链 → {real}")
                info = classify(real)
            if info["kind"] == "video":
                self.log(f"解析作品 {info['aweme_id']} …")
                aweme = br.fetch_aweme(info["aweme_id"])
                self.log(f"《{dl.safe_name(dl.aweme_meta(aweme)['desc'], 30)}》 @{dl.aweme_meta(aweme)['author']}")
                self.on_progress(1, 1, "单条作品")
                self.save_aweme(aweme)
            elif info["kind"] == "user":
                limit = int(self.cfg.get("max_user_videos", 50) or 0)
                self.log(f"抓取博主主页作品（上限 {limit or '全部'} 条）…")
                posts = br.fetch_user_posts(info["sec_uid"], limit=limit)
                self.log(f"共拿到 {len(posts)} 条作品，开始下载")
                for i, aweme in enumerate(posts, 1):
                    if self._stop():
                        self.log("已手动停止下载")
                        break
                    meta = dl.aweme_meta(aweme)
                    label = f"{meta['author']} · {dl.safe_name(meta['desc'], 30)}"
                    self.on_progress(i - 1, len(posts), label)
                    self.log(f"[{i}/{len(posts)}] {meta['author']} · {dl.safe_name(meta['desc'], 40)}")
                    try:
                        self.save_aweme(aweme)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"  × 失败：{exc}")
                        self.stats["failed"] += 1
                self.on_progress(len(posts), len(posts), "完成")
            else:
                self.log(f"× 不认识的链接类型：{url}")

    # ---------- 汇总 ----------
    def summary(self) -> str:
        s = self.stats
        extra = f"，重新下载 {s.get('redo', 0)} 条" if s.get("redo") else ""
        moved = f"，归位 {s.get('moved', 0)} 条" if s.get("moved") else ""
        return (
            f"完成：视频 {s['video']} 条，图文 {s['image']} 组，"
            f"跳过 {s['skip']} 条{extra}{moved}，失败 {s['failed']} 条"
        )
