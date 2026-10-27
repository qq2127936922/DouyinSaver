"""用真实浏览器抓取抖音页面数据（无需登录账号，仅用浏览器自带 Cookie 通过风控）。

思路：
  - 打开 www.douyin.com 的作品页 / 用户主页，拦截网页自身发出的
    aweme/detail、aweme/post 接口 JSON（浏览器会自动带上合法签名），
    从而拿到无水印播放地址、图集图片、背景音乐等原始数据。
  - 浏览器 Profile 持久化在 .browser 目录，Cookie 复用，减少验证码。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 登录后浏览器会多出这些 Cookie，用来判断是否已登录
LOGIN_COOKIES = ("sessionid", "sessionid_ss")


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------- 登录状态
def profile_dir(download_dir=None) -> Path:
    """浏览器 Profile 固定在程序目录下，换下载目录也不用重新登录。

    老版本放在 downloads/.browser，这里顺手搬一次。
    """
    from .config import ROOT

    target = ROOT / ".browser"
    if not target.exists() and download_dir:
        old = Path(download_dir) / ".browser"
        if old.exists():
            try:
                shutil.move(str(old), str(target))
            except Exception:  # noqa: BLE001
                return old
    return target


def _ctx_logged_in(ctx) -> bool:
    try:
        for c in ctx.cookies():
            if c.get("name") in LOGIN_COOKIES and c.get("value"):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _cookie_dbs(prof: Path) -> list[Path]:
    """新版 Chromium/Edge 的 Cookie 库搬到了 Default/Network/。"""
    return [Path(prof) / "Default" / "Network" / "Cookies", Path(prof) / "Default" / "Cookies"]


def _count_login_cookies(db: Path) -> int:
    names = ",".join(f"'{n}'" for n in LOGIN_COOKIES)
    sql = f"select count(*) from cookies where name in ({names})"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True, timeout=3)
    try:
        return int((con.execute(sql).fetchone() or [0])[0])
    finally:
        con.close()


def has_login(prof: Path) -> bool:
    """不启动浏览器，直接读 Profile 里的 Cookie 库判断有没有登录过。"""
    for db in _cookie_dbs(Path(prof)):
        if not db.exists() or db.stat().st_size == 0:
            continue
        try:
            if _count_login_cookies(db):
                return True
            continue
        except Exception:  # noqa: BLE001
            pass
        # 库被浏览器占用（或数据还在 WAL 里）→ 复制一份再读
        tmpdir = Path(tempfile.mkdtemp())
        try:
            shutil.copy2(db, tmpdir / "Cookies")
            for ext in ("-wal", "-shm"):
                side = Path(str(db) + ext)
                if side.exists():
                    shutil.copy2(side, tmpdir / ("Cookies" + ext))
            con = sqlite3.connect(str(tmpdir / "Cookies"), timeout=5)
            try:
                names = ",".join(f"'{n}'" for n in LOGIN_COOKIES)
                row = con.execute(f"select count(*) from cookies where name in ({names})").fetchone()
            finally:
                con.close()
            if row and row[0]:
                return True
        except Exception:  # noqa: BLE001
            continue
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return False


COLLECT_FOLDERS_API = "/aweme/v1/web/collects/list/"
COLLECT_ITEMS_API = "/aweme/v1/web/collects/video/list/"

# 点开收藏夹的两个坑（都是实测踩出来的）：
#   1) 抖音不认页面里 dispatchEvent 造的合成事件（isTrusted=false，点完右边一动不动），
#      必须用 Playwright 的真点击；
#   2) 卡片所在列表会被滚动到很远的地方（铺卡片阶段滚到底后，卡片跑到视口上方 2700px），
#      所以别自己算坐标，直接把元素句柄交给 Playwright——它会自己把元素滚进视口再点。
# 元素每次现找：列表重渲染后旧句柄会失效（点的时候报 not attached，重找即可，不浪费时间等）。
_FIND_FOLDER_JS = """(name) => {
  const key = (name || '').slice(0, 4);
  if (!key) return null;
  const cands = [...document.querySelectorAll('li,div,a')].filter(e => {
    const t = (e.innerText || '').trim();
    if (!t.startsWith(key)) return false;
    if (t.length > 80) return false;
    const r = e.getBoundingClientRect();
    return r.width > 70 && r.width < 640 && r.height > 30 && r.height < 320;
  });
  if (!cands.length) return null;
  cands.sort((a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const pa = a.tagName === 'LI' ? 0 : 1, pb = b.tagName === 'LI' ? 0 : 1;
    return (pa - pb) || (ra.width * ra.height - rb.width * rb.height);
  });
  return cands[0];
}"""

# 把「装着收藏夹卡片的那条滚动列表」记到 window.__dsyList，之后翻找卡片只滚它，
# 别去动右边作品区（动了会把卡片顶出视口，越找越找不到）。
_LIST_OF_JS = """(name) => {
  const card = (() => {
    const key = (name || '').slice(0, 4);
    return [...document.querySelectorAll('li,div,a')].find(e => {
      const t = (e.innerText || '').trim();
      return t.startsWith(key) && t.length < 80;
    });
  })();
  if (!card) return false;
  let node = card.parentElement;
  while (node && node !== document.body) {
    if (node.scrollHeight > node.clientHeight + 50 && node.clientHeight > 200) {
      window.__dsyList = node;
      return true;
    }
    node = node.parentElement;
  }
  return false;
}"""

# 抖音主页的作品列表放在一个内部滚动容器里（.route-scroll-container 之类），
# 直接滚 window 经常一动不动，翻页自然就停住了。这里把所有能滚的容器都推到最底。
_SCROLL_JS = """() => {
  const boxes = Array.from(document.querySelectorAll('*')).filter(
    e => e.scrollHeight > e.clientHeight + 200 && e.clientHeight > 300
  );
  let moved = 0;
  for (const e of boxes) {
    const before = e.scrollTop;
    e.scrollTop = e.scrollHeight;
    if (e.scrollTop !== before) moved++;
  }
  if (!moved) window.scrollTo(0, document.documentElement.scrollHeight);
  return moved;
}"""


class DouyinBrowser:
    def __init__(self, profile_dir: str | Path, headless: bool = True, log=None):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.log = log or (lambda msg: None)
        self._pw = None
        self._ctx = None
        self._warmed = False
        self.logged_in = False

    # ---------- 生命周期 ----------
    def _launch(self, channel: str | None, headless: bool | None = None):
        kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless if headless is None else headless,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=DEFAULT_UA,
            viewport={"width": 1440, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--mute-audio",
                # 让页面自己把音乐放起来，好顺手截获音频流（有些作品的直链是空的）
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        if channel:
            kwargs["channel"] = channel
        return self._pw.chromium.launch_persistent_context(**kwargs)

    def _open(self, headless: bool | None = None):
        # 优先用系统已装的 Edge / Chrome（不用另外下载内核），实在没有再回退到自带内核
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        last = None
        for ch in ("msedge", "chrome", None):
            try:
                ctx = self._launch(ch, headless=headless)
                self.log(f"浏览器内核：{ch or 'playwright 自带 chromium'}")
                return ctx
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise RuntimeError(f"没有可用的浏览器内核：{last}")

    def __enter__(self):
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._open()
        except Exception:
            self._pw.stop()
            raise
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        self.logged_in = _ctx_logged_in(self._ctx)
        if self.logged_in:
            self.log("账号状态：已登录")
        else:
            self.log("账号状态：未登录（博主主页通常只返回前 18 条作品）")
        return self

    def __exit__(self, exc_type, exc, tb):
        # 抖音页面挂久了 ctx.close() 会一直卡着（实测 140 秒都不返回，直接吊死进程），
        # 但 page.close() 是毫秒级、pw.stop() 30 秒内必然结束（它会强杀浏览器进程），
        # 所以这里不调 ctx.close()，关页面后直接停 Playwright。
        try:
            if self._ctx:
                for p in list(self._ctx.pages):
                    try:
                        p.close(run_before_unload=False)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self._pw:
                try:
                    self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None
            self._ctx = None

    # ---------- 内部 ----------
    def _page(self):
        page = self._ctx.new_page()
        page.set_default_timeout(30000)
        return page

    def warmup(self):
        """先访问首页拿到 ttwid 等基础 Cookie。"""
        if self._warmed:
            return
        page = self._page()
        try:
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1800)
            self.log("浏览器就绪（已获取风控 Cookie）")
        except Exception as exc:  # noqa: BLE001
            self.log(f"首页预热失败，继续尝试：{exc}")
        finally:
            page.close()
            self._warmed = True

    def resolve_short(self, url: str) -> str:
        """短链 v.douyin.com/xxx → 真实地址。"""
        page = self._page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1200)
            return page.url
        finally:
            page.close()

    # ---------- 作品详情 ----------
    def fetch_aweme(self, aweme_id: str, retries: int = 2) -> dict:
        self.warmup()
        last_err = None
        for attempt in range(1, retries + 2):
            box: dict = {}
            page = self._page()

            def on_response(resp):
                try:
                    ctype = (resp.headers or {}).get("content-type", "")
                    if "audio" in ctype and "images_no_sound" not in resp.url:
                        # 页面自己播放的音乐流，给版权音乐兜底用
                        if "music" not in box:
                            box["music"] = (resp.url, ".m4a" if "mp4" in ctype else ".mp3")
                except Exception:  # noqa: BLE001
                    pass
                if "/aweme/v1/web/aweme/detail/" in resp.url:
                    try:
                        data = resp.json()
                    except Exception:  # noqa: BLE001
                        return
                    detail = data.get("aweme_detail")
                    if detail:
                        box["detail"] = detail

            page.on("response", on_response)
            try:
                page.goto(
                    f"https://www.douyin.com/video/{aweme_id}",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                for _ in range(40):
                    if "detail" in box:
                        break
                    page.wait_for_timeout(500)
                if "detail" not in box:
                    # 有些作品页要滚动后才触发请求
                    page.mouse.wheel(0, 1200)
                    for _ in range(16):
                        if "detail" in box:
                            break
                        page.wait_for_timeout(500)
                if "detail" in box:
                    detail = box["detail"]
                    music = detail.get("music") or {}
                    has_direct = bool((music.get("play_url") or {}).get("url_list"))
                    if not has_direct and "music" not in box:
                        # 版权音乐接口不给直链，页面要播一会儿才发音频请求，等一下截它
                        for _ in range(24):
                            if "music" in box:
                                break
                            page.wait_for_timeout(500)
                    if "music" in box:
                        detail["_fallback_music"] = box["music"]
                    return detail
                last_err = "页面未返回作品数据（可能被风控或作品不可见）"
                self.log(f"第 {attempt} 次抓取失败：{last_err}")
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                self.log(f"第 {attempt} 次抓取异常：{exc}")
            finally:
                page.close()
            time.sleep(1.5)
        raise FetchError(last_err or "抓取失败")

    # ---------- 我的收藏 / 点赞 ----------
    def fetch_my_list(self, kind: str = "collect", limit: int = 0, max_idle: int = 6) -> list[dict]:
        """抓我自己的「收藏」(collect) 或「点赞」(like)，按抖音的默认顺序（新的在前）。

        - 收藏：POST /aweme/v1/web/aweme/listcollection/（每页 7~10 条）
        - 点赞：GET  /aweme/v1/web/aweme/favorite/（每页 17~19 条）
        必须处于登录状态，否则只能拿到空列表。
        """
        self.warmup()
        if not self.logged_in:
            raise FetchError("抓收藏/点赞需要先扫码登录")

        api = (
            "/aweme/v1/web/aweme/listcollection/"
            if kind == "collect"
            else "/aweme/v1/web/aweme/favorite/"
        )
        label = "收藏" if kind == "collect" else "点赞"
        items: list[dict] = []
        seen: set[str] = set()
        state = {"has_more": True}

        def on_response(resp):
            if api not in resp.url:
                return
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                return
            if not isinstance(data, dict):
                return
            for aweme in data.get("aweme_list") or []:
                if not isinstance(aweme, dict):
                    continue
                aid = str(aweme.get("aweme_id") or "")
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                items.append(aweme)
            if "has_more" in data:
                state["has_more"] = bool(data.get("has_more"))

        page = self._page()
        page.on("response", on_response)
        try:
            page.goto(
                "https://www.douyin.com/user/self?showTab=favorite_collection",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(3500)
            # 收藏页要先点「收藏」再点子 tab「视频」，列表才会开始请求
            clicks = ("收藏", "视频") if kind == "collect" else ("喜欢",)
            for text in clicks:
                try:
                    page.locator(f'span:text-is("{text}")').first.click(timeout=2000)
                    page.wait_for_timeout(2500)
                except Exception:  # noqa: BLE001
                    continue

            idle = 0
            while True:
                if limit and len(items) >= limit:
                    break
                before = len(items)
                try:
                    page.evaluate(_SCROLL_JS)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(1500)
                if len(items) > before:
                    idle = 0
                    self.log(f"  已获取 {len(items)} 条{label}…")
                    continue
                idle += 1
                if not state["has_more"] or idle >= max_idle:
                    break
        finally:
            page.close()

        if limit:
            items = items[:limit]
        self.log(f"  {label}列表共 {len(items)} 条")
        return items

    # ---------- 我的收藏夹 ----------
    # 收藏夹列表单独记了引用（window.__dsyList），翻找卡片只滚它；
    # 没记到的时候退回「滚所有窄容器」，兼容页面刚打开还没铺卡片的情况。
    _RESET_LIST_JS = """() => {
      if (window.__dsyList && document.contains(window.__dsyList)) {
        window.__dsyList.scrollTop = 0;
        return 1;
      }
      const bs = [...document.querySelectorAll('*')].filter(
        e => e.scrollHeight > e.clientHeight + 200 && e.clientHeight > 300 && e.clientWidth < 620);
      bs.forEach(b => { b.scrollTop = 0; });
      return bs.length;
    }"""

    _NUDGE_LIST_JS = """(d) => {
      if (window.__dsyList && document.contains(window.__dsyList)) {
        window.__dsyList.scrollTop += d;
        return 1;
      }
      const bs = [...document.querySelectorAll('*')].filter(
        e => e.scrollHeight > e.clientHeight + 200 && e.clientHeight > 300 && e.clientWidth < 620);
      bs.forEach(b => { b.scrollTop += d; });
      return bs.length;
    }"""

    # 作品列表在右边那块宽容器里（有时是 route 容器本身在滚），翻页只滚宽的，
    # 左栏那条窄的收藏夹列表别动，不然下一个收藏夹卡片就被滚没影了
    _SCROLL_RIGHT_JS = """() => {
      const bs = [...document.querySelectorAll('*')].filter(
        e => e.scrollHeight > e.clientHeight + 120 && e.clientHeight > 260 && e.clientWidth > 560);
      let moved = 0;
      for (const b of bs) {
        const before = b.scrollTop;
        b.scrollTop = b.scrollHeight;
        if (b.scrollTop !== before) moved++;
      }
      return moved;
    }"""

    def _click_folder(
        self, page, name: str, reset: bool = False, tries: int = 2, budget: float = 22.0
    ) -> bool:
        """点开左栏里的某个收藏夹（点成功后右边会展示它的作品）。

        做法：现找元素句柄 → 交给 Playwright 点（真事件 + 自动滚进视口）。
        句柄失效（not attached）就重找；个别卡片怎么都点不动时靠 budget 兜住，
        不让它把整个流程拖住（超时就先收首屏，继续下一个收藏夹）。
        """
        try:
            page.evaluate(_LIST_OF_JS, name)   # 认一下收藏夹列表是哪条容器，翻找只滚它
        except Exception:  # noqa: BLE001
            pass
        deadline = time.time() + budget
        for attempt in range(tries):
            if time.time() > deadline:
                return False
            if attempt or reset:
                try:
                    page.evaluate(self._RESET_LIST_JS)
                    page.wait_for_timeout(500)
                except Exception:  # noqa: BLE001
                    pass
            for _ in range(20):
                if time.time() > deadline:
                    return False
                try:
                    handle = page.evaluate_handle(_FIND_FOLDER_JS, name)
                    el = handle.as_element()
                except Exception:  # noqa: BLE001
                    el = None
                if el is not None:
                    try:
                        el.click(timeout=2500)
                        return True
                    except Exception:  # noqa: BLE001
                        pass          # 多半是重渲染把节点换掉了，重找一次
                try:
                    page.evaluate(self._NUDGE_LIST_JS, 380)   # 往下翻着找
                except Exception:  # noqa: BLE001
                    return False
                page.wait_for_timeout(300)
        return False

    def collect_folders(
        self,
        full: bool = False,
        on_items=None,
        on_folder=None,
        stop=None,
        limit_folders: int = 0,
        cache_path: Path | None = None,
    ) -> dict:
        """遍历「我的 → 收藏 → 收藏夹」，按抖音里的收藏夹分组拿作品。

        full=False：只取每个收藏夹的首屏（页面铺卡片时自动请求的第一页，约 10 条），
                    几十秒跑完，足够覆盖手机上刚点的新收藏。
        full=True ：逐个点开收藏夹、滚到底，把历史作品也拿全（用于整理旧备份）。

        on_items(folder, awemes) 每拿到一个收藏夹就回调，处理完即可释放内存。
        cache_path 给了就边抓边写（万一中途卡住/停掉，已抓到的还能用）。
        返回 {"folders": [{id,name,count}], "id2folder": {aweme_id: 收藏夹名},
              "got": {收藏夹名: 抓到条数}}
        """
        self.warmup()
        if not self.logged_in:
            raise FetchError("抓收藏夹需要先扫码登录（点界面上的「扫码登录」）")

        folders: list[dict] = []
        seen_folder: set[str] = set()
        items: dict[str, dict[str, dict]] = {}
        more: dict[str, bool] = {}
        hits: dict[str, int] = {}
        folders_more = {"has": True}

        def on_response(resp):
            u = resp.url
            if COLLECT_FOLDERS_API in u:
                try:
                    data = resp.json() or {}
                except Exception:  # noqa: BLE001
                    return
                for f in data.get("collects_list") or []:
                    if not isinstance(f, dict):
                        continue
                    fid = str(f.get("collects_id_str") or f.get("collects_id") or "")
                    if not fid or fid in seen_folder:
                        continue
                    seen_folder.add(fid)
                    folders.append({
                        "id": fid,
                        "name": str(f.get("collects_name") or "未命名收藏夹").strip(),
                        "count": int(f.get("total_number") or 0),
                    })
                if "has_more" in data:
                    folders_more["has"] = bool(data.get("has_more"))
            elif COLLECT_ITEMS_API in u:
                try:
                    cid = u.split("collects_id=")[1].split("&")[0]
                except Exception:  # noqa: BLE001
                    return
                try:
                    data = resp.json() or {}
                except Exception:  # noqa: BLE001
                    return
                box = items.setdefault(cid, {})
                for aweme in data.get("aweme_list") or []:
                    if not isinstance(aweme, dict):
                        continue
                    aid = str(aweme.get("aweme_id") or "")
                    if aid:
                        box[aid] = aweme
                if "has_more" in data:
                    more[cid] = bool(data.get("has_more"))
                hits[cid] = hits.get(cid, 0) + 1

        def stopped() -> bool:
            return bool(stop and stop())

        page = self._page()
        page.on("response", on_response)
        id2folder: dict[str, str] = {}
        got: dict[str, int] = {}

        def flush(folder: dict) -> None:
            cid = folder["id"]
            box = items.pop(cid, {})
            more.pop(cid, None)
            for aid in box:
                id2folder.setdefault(aid, folder["name"])
            got[folder["name"]] = len(box)
            if cache_path:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(
                        json.dumps({"ts": time.time(), "folders": folders, "map": id2folder},
                                   ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception:  # noqa: BLE001
                    pass
            if on_items and box:
                on_items(folder, list(box.values()))

        # 首屏铺卡片：每个收藏夹会自动请求一页
        try:
            page.goto(
                "https://www.douyin.com/user/self?showTab=favorite_collection",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(5200)
            for text in ("收藏", "收藏夹"):
                try:
                    page.locator(f'span:text-is("{text}"), div:text-is("{text}")').first.click(timeout=3500)
                    page.wait_for_timeout(2600)
                except Exception:  # noqa: BLE001
                    continue
            if not folders:
                self.log("没有找到收藏夹，可能还没登录或抖音页面结构变了")
                return {"folders": [], "id2folder": {}, "got": {}}

            self.log(f"发现 {len(folders)} 个收藏夹")
            self.log("  " + "、".join(f["name"] for f in folders[:12]) + ("…" if len(folders) > 12 else ""))

            if not full:
                # 只铺卡片：滚几轮把收藏夹列表加载完（顺带每个收藏夹的第一页都到手）
                idle = 0
                for _ in range(40):
                    if stopped():
                        break
                    before = len(folders)
                    try:
                        page.evaluate(_SCROLL_JS)
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(1500)
                    if len(folders) > before:
                        idle = 0
                    else:
                        idle += 1
                        if idle >= 3 and not folders_more["has"]:
                            break
                        if idle >= 5:
                            break
                # 最后几个卡片的封面请求可能还在路上，等一会儿再收
                page.wait_for_timeout(1800)
                for f in folders:
                    if stopped():
                        break
                    flush(f)
                return {"folders": folders, "id2folder": id2folder, "got": got}

            # 全量：先确认收藏夹列表已加载完，再逐个点开翻到底
            idle = 0
            for _ in range(40):
                if stopped():
                    break
                before = len(folders)
                try:
                    page.evaluate(_SCROLL_JS)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(1400)
                if len(folders) > before:
                    idle = 0
                else:
                    idle += 1
                    if idle >= 3 and not folders_more["has"]:
                        break
                    if idle >= 6:
                        break

            targets = folders[:limit_folders] if limit_folders else folders
            prev_ok = False
            for i, f in enumerate(targets, 1):
                if stopped():
                    self.log("已停止")
                    break
                if on_folder:
                    on_folder(f)
                # 上一个收藏夹可能把左栏滚走了；第一次从头找，后面接着往下找
                cid = f["id"]
                opened = False
                for attempt in range(2):
                    if not self._click_folder(page, f["name"], reset=(attempt > 0 or not prev_ok)):
                        continue
                    # 点中后右边会立刻换成这个收藏夹的内容（可能走缓存、不发请求），
                    # 所以不能靠「有没有新请求」判断，直接滚一轮看看数据会不会变多
                    page.wait_for_timeout(1500)
                    n_first = len(items.get(cid, {}))
                    idle = 0
                    for _ in range(45):
                        if stopped():
                            break
                        n0 = len(items.get(cid, {}))
                        try:
                            page.evaluate(self._SCROLL_RIGHT_JS)   # 只滚右边作品区
                        except Exception:  # noqa: BLE001
                            pass
                        page.wait_for_timeout(1200)
                        n1 = len(items.get(cid, {}))
                        if n1 > n0:
                            idle = 0
                        else:
                            idle += 1
                            if not more.get(cid, False) or idle >= 4:
                                break
                    if len(items.get(cid, {})) > n_first or not more.get(cid, True):
                        opened = True
                        break
                    self.log(f"    「{f['name']}」没翻出新内容，再点一次")
                if not opened:
                    self.log(f"  [{i}/{len(targets)}] {f['name']}：没能翻页，先只收首屏")
                    prev_ok = False
                    flush(f)
                    continue
                prev_ok = True
                flush(f)
                self.log(
                    f"  [{i}/{len(targets)}] {f['name']}：{got.get(f['name'], 0)} 条"
                    + ("" if not f["count"] or got.get(f["name"], 0) >= f["count"] else f"（抖音显示 {f['count']}）")
                )
        finally:
            try:
                page.close(run_before_unload=False)
            except Exception:  # noqa: BLE001
                pass

        return {"folders": folders, "id2folder": id2folder, "got": got}

    # ---------- 作者全部作品 ----------
    def fetch_user_posts(self, sec_uid: str, limit: int = 0, on_batch=None) -> list[dict]:
        """limit<=0 表示尽量抓全（受抖音网页端限制，通常最多几百条）。"""
        self.warmup()
        items: list[dict] = []
        seen: set[str] = set()
        state = {"has_more": True}
        page = self._page()

        def collect(data):
            for aweme in data.get("aweme_list") or []:
                aid = str(aweme.get("aweme_id") or "")
                if not aid or aid in seen:
                    continue
                # 主页作品列表里带 is_top / 置顶，图片集也在其中
                seen.add(aid)
                items.append(aweme)
            if "has_more" in data:
                state["has_more"] = bool(data.get("has_more"))
            if on_batch and items:
                on_batch(list(items))

        def on_response(resp):
            if "/aweme/v1/web/aweme/post/" in resp.url:
                try:
                    collect(resp.json())
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", on_response)
        try:
            page.goto(
                f"https://www.douyin.com/user/{sec_uid}",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(3000)
            # 关掉可能出现的登录弹窗
            try:
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass

            idle_rounds = 0
            while True:
                if limit and len(items) >= limit:
                    break
                before = len(items)
                # 一次推到列表底部（内部容器），再补一次滚轮兜底
                try:
                    page.evaluate(_SCROLL_JS)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    page.mouse.move(760, 620)
                    page.mouse.wheel(0, 3000)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(1500)

                if len(items) > before:
                    idle_rounds = 0
                    self.log(f"已获取 {len(items)} 条作品…")
                    continue
                # 这一轮没长：可能只是网络慢，多等两轮；接口明确说没有下一页就收工
                idle_rounds += 1
                if not state["has_more"] or idle_rounds >= 6:
                    if not self.logged_in and len(items) <= 20:
                        self.log(
                            f"只拿到 {len(items)} 条：抖音未登录时主页只给前 18 条左右。"
                            "点界面上的「扫码登录」再跑一次就能抓全"
                        )
                    else:
                        self.log(f"翻页到底了，共 {len(items)} 条")
                    break
        finally:
            page.close()

        if limit:
            items = items[:limit]
        if not items:
            raise FetchError("没有抓到该作者的作品（可能需要登录或主页受限）")
        return items


# ---------------------------------------------------------------- 扫码登录
def qr_login(prof: Path, log=None, timeout: int = 300, stop=None, headless: bool = False) -> bool:
    """打开一个真实浏览器窗口，让用户用抖音 App 扫码登录，Cookie 存进同一个 Profile。

    只需做一次；之后所有下载都用这份登录态（想退出登录删掉 .browser 目录即可）。
    """
    log = log or (lambda msg: None)
    prof = Path(prof)

    def stopped() -> bool:
        return bool(stop and stop())

    pw = sync_playwright().start()
    ctx = None
    try:
        prof.mkdir(parents=True, exist_ok=True)
        last = None
        for ch in ("msedge", "chrome", None):
            try:
                kwargs = dict(
                    user_data_dir=str(prof),
                    headless=headless,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    viewport={"width": 1180, "height": 820},
                    args=["--disable-blink-features=AutomationControlled", "--mute-audio"],
                )
                if ch:
                    kwargs["channel"] = ch
                ctx = pw.chromium.launch_persistent_context(**kwargs)
                log(f"已打开浏览器（内核：{ch or 'playwright 自带 chromium'}）")
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
        if ctx is None:
            log(f"打不开浏览器：{last}")
            return False

        if _ctx_logged_in(ctx):
            log("当前已经是登录状态，不需要重新扫码")
            return True

        pages = ctx.pages
        page = pages[0] if pages else ctx.new_page()
        page.set_default_timeout(20000)
        try:
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:  # noqa: BLE001
            log(f"打开抖音首页超时（不影响扫码）：{exc}")

        page.wait_for_timeout(2000)
        if _ctx_logged_in(ctx):
            log("当前已经是登录状态，不需要重新扫码")
            return True

        # 尽量把「扫码登录」的二维码点出来
        for sel in (
            'div[data-e2e="login-button"]',
            'button:has-text("登录")',
            'div:has-text("扫码登录")',
            'span:has-text("登录")',
        ):
            try:
                page.click(sel, timeout=900)
                page.wait_for_timeout(700)
                break
            except Exception:  # noqa: BLE001
                continue

        log("请在打开的浏览器窗口里用「抖音 App → 扫一扫」扫码登录")
        log("登录成功后窗口会自动关闭；不想登录直接关掉窗口即可")
        t0 = time.time()
        while time.time() - t0 < timeout:
            if stopped():
                log("已取消登录")
                return False
            if _ctx_logged_in(ctx):
                log("登录成功，登录状态已保存（以后不用再扫）")
                page.wait_for_timeout(1500)
                return True
            try:
                if not ctx.pages:
                    log("浏览器窗口已关闭，取消登录")
                    return False
            except Exception:  # noqa: BLE001
                log("浏览器窗口已关闭，取消登录")
                return False
            try:
                page.wait_for_timeout(1200)
            except Exception:  # noqa: BLE001
                return False
        mins = max(1, timeout // 60) if timeout >= 60 else 0
        log(
            f"等待扫码超时（{str(mins) + ' 分钟' if mins else str(timeout) + ' 秒'}），"
            "可以再点一次「扫码登录」"
        )
        return False
    finally:
        try:
            if ctx:
                ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass

