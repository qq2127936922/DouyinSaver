"""保存到百度网盘。

两种方式：
1. copy —— 把文件放进「百度网盘客户端」的本地同步文件夹，客户端自动上传。
2. api  —— 走百度网盘开放平台（xpan 接口）直传，不需要装客户端。

开放平台有两条硬规则（2026 年版）：
- 应用只能在自己那一个目录里读写：`/apps/{应用名}`（网盘里显示为「我的应用数据/{应用名}」）
- 所以最终路径是 `/apps/{应用名}/{remote_dir}/{作者}/{标题}/文件`
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

# 百度网盘对路径里的字符有白名单（emoji 之类直接报 errno=-7「非法字符」），
# 上传/建目录/列目录前统一把不认的字符剔掉，本地文件名保持原样不受影响
# 白名单只放常见的：中日韩文字、英数、空格和常用标点。
# 不能用 \w —— 它会放过生僻的部首符号（如 𐋣，百度照样报 errno=-7）
_PAN_ILLEGAL = re.compile(
    r"[^A-Za-z0-9_一-鿿㐀-䶿぀-ヿ가-힣"
    r"\s（）()#：?？!！.，,、\-—~…：·「」『』／]"
)


def pan_safe_path(path: str) -> str:
    """把网盘路径里不认的字符去掉（按 / 分段净化，保留开头的 /）。"""
    text = str(path)
    lead = "/" if text.startswith("/") else ""
    segs = []
    for seg in text.split("/"):
        seg = _PAN_ILLEGAL.sub("", seg).strip()
        if seg:
            segs.append(seg)
    return lead + "/".join(segs)

import requests

BLOCK = 4 * 1024 * 1024

OAUTH_AUTHORIZE = "https://openapi.baidu.com/oauth/2.0/authorize"
OAUTH_TOKEN = "https://openapi.baidu.com/oauth/2.0/token"
XPAN_FILE = "https://pan.baidu.com/rest/2.0/xpan/file"
XPAN_MULTIMEDIA = "https://pan.baidu.com/rest/2.0/xpan/multimedia"
SUPERFILE = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
DEFAULT_REDIRECT = "http://localhost:8899/callback"
SCOPE = "basic,netdisk"


def _md5_blocks(path: Path) -> list[str]:
    blocks = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(BLOCK)
            if not chunk:
                break
            blocks.append(hashlib.md5(chunk).hexdigest())
    return blocks


# ---------------------------------------------------------------- OAuth
def authorize_url(app_key: str, redirect_uri: str = DEFAULT_REDIRECT) -> str:
    q = urlencode(
        {
            "response_type": "code",
            "client_id": app_key,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "display": "page",
        }
    )
    return f"{OAUTH_AUTHORIZE}?{q}"


def _token_call(params: dict) -> dict:
    try:
        r = requests.get(OAUTH_TOKEN, params=params, timeout=30)
        return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": "request_failed", "error_description": str(exc)}


def exchange_code(app_key: str, secret_key: str, code: str, redirect_uri: str = DEFAULT_REDIRECT) -> dict:
    """用授权码换 access_token / refresh_token。"""
    return _token_call(
        {
            "grant_type": "authorization_code",
            "code": code.strip(),
            "client_id": app_key,
            "client_secret": secret_key,
            "redirect_uri": redirect_uri,
        }
    )


def refresh_access_token(app_key: str, secret_key: str, refresh_token: str) -> dict:
    return _token_call(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": app_key,
            "client_secret": secret_key,
        }
    )


def wait_for_code(redirect_uri: str, timeout: int = 240, stop=None, log=None) -> str | None:
    """起一个本地 HTTP 服务接 OAuth 回调，拿到 code 就返回。"""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    log = log or (lambda m: None)
    u = urlparse(redirect_uri)
    port = u.port or 8899
    box: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            if "code" in q and q["code"]:
                box["code"] = q["code"][0]
                body = "<meta charset=utf-8><h2>授权成功</h2><p>可以关闭本页，回到程序了。</p>"
            else:
                body = "<meta charset=utf-8><h2>没有拿到授权码</h2><p>请重新在程序里点授权。</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *args):  # noqa: A003
            pass

    try:
        srv = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        log(f"本地回调端口 {port} 被占用（{exc}），改用粘贴授权码的方式")
        return None
    srv.timeout = 1
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            if stop and stop():
                return None
            if box.get("code"):
                return box["code"]
            srv.handle_request()
        return None
    finally:
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass


def login(
    app_key: str,
    secret_key: str,
    redirect_uri: str = DEFAULT_REDIRECT,
    log=None,
    timeout: int = 240,
    stop=None,
    open_browser: bool = True,
) -> tuple[bool, str, dict]:
    """带浏览器跳转的完整授权流程。返回 (成功?, 说明, token 信息)。"""
    log = log or (lambda m: None)
    if not app_key or not secret_key:
        return False, "还没填 AppKey / SecretKey", {}

    # 先起服务再开浏览器，避免回调来得太快收不到
    import threading

    holder: dict = {}
    url = authorize_url(app_key, redirect_uri)

    def run_waiter():
        holder["code"] = wait_for_code(redirect_uri, timeout=timeout, stop=stop, log=log)

    th = threading.Thread(target=run_waiter, daemon=True)
    th.start()
    time.sleep(0.3)
    log(f"已打开百度授权页面，请用百度账号登录并点「授权」：{url[:80]}…")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            log("自动打开浏览器失败，请手动复制上面的链接")
    th.join(timeout=timeout + 5)
    code = holder.get("code")
    if not code:
        return False, "没收到授权结果（可以改用「粘贴授权码」）", {}

    data = exchange_code(app_key, secret_key, code, redirect_uri)
    if not data.get("access_token"):
        return False, f"换 token 失败：{data.get('error_description') or data}", {}
    log("授权成功，已拿到 access_token")
    return True, "授权成功", data


# ---------------------------------------------------------------- 网盘
class BaiduPan:
    def __init__(
        self,
        mode: str = "copy",
        sync_dir: str = "",
        access_token: str = "",
        remote_dir: str = "抖音备份",
        log=None,
        app_name: str = "",
        app_key: str = "",
        secret_key: str = "",
        refresh_token: str = "",
        expires_at: float = 0,
        redirect_uri: str = DEFAULT_REDIRECT,
        on_token=None,
    ):
        self.mode = (mode or "copy").lower()
        self.sync_dir = Path(sync_dir) if sync_dir else None
        self.token = (access_token or "").strip()
        self.remote_dir = (remote_dir or "").strip().strip("/")
        self.log = log or (lambda m: None)
        self.app_name = (app_name or "").strip().strip("/")
        self.app_key = (app_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.refresh_token = (refresh_token or "").strip()
        self.expires_at = float(expires_at or 0)
        self.redirect_uri = redirect_uri or DEFAULT_REDIRECT
        self.on_token = on_token

    # ---- 路径 ----
    def remote_root(self) -> str:
        """网盘里的根目录：/apps/{应用名}/{文件夹}

        应用名和文件夹重名时只算一层（老版本就是这么传的，
        网盘上已经是 /apps/抖音备份/作者/作品，别再多套一层）。
        """
        base = f"/apps/{self.app_name}" if self.app_name else "/apps"
        if not self.remote_dir or self.remote_dir == self.app_name:
            return base
        return f"{base}/{self.remote_dir}"

    # ---- token ----
    def _save_token(self) -> None:
        if not self.on_token:
            return
        try:
            self.on_token(
                {
                    "access_token": self.token,
                    "refresh_token": self.refresh_token,
                    "expires_at": self.expires_at,
                }
            )
        except Exception:  # noqa: BLE001
            pass

    def _refresh(self) -> bool:
        if not (self.app_key and self.secret_key and self.refresh_token):
            return False
        data = refresh_access_token(self.app_key, self.secret_key, self.refresh_token)
        if not data.get("access_token"):
            self.log(f"刷新网盘 token 失败：{data.get('error_description') or data}")
            return False
        self.token = data["access_token"]
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        self.expires_at = time.time() + float(data.get("expires_in") or 2592000)
        self._save_token()
        return True

    def ensure_token(self) -> None:
        if not self.token:
            raise RuntimeError("还没授权百度网盘，请在「网盘」里点授权")
        # 提前一天续期，避免下好文件才发现 token 过期
        if self.expires_at and time.time() > self.expires_at - 86400:
            self.log("网盘 token 快过期了，自动续期…")
            self._refresh()

    # ---------------- 方式一：同步文件夹 ----------------
    def _save_to_sync_dir(self, local: Path, subdir: str) -> str:
        if not self.sync_dir:
            raise RuntimeError("未设置百度网盘同步文件夹路径")
        target_dir = self.sync_dir / subdir if subdir else self.sync_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / local.name
        if target.exists():
            return str(target)
        shutil.move(str(local), str(target))
        return str(target)

    # ---------------- 方式二：开放平台 API ----------------
    def _mkdir(self, remote_path: str) -> None:
        try:
            requests.post(
                XPAN_FILE,
                params={"method": "create", "access_token": self.token},
                data={"path": remote_path, "isdir": 1, "rtype": 3},
                timeout=30,
            )
        except Exception:  # noqa: BLE001
            pass

    def _ensure_dir(self, remote_path: str) -> None:
        cur = ""
        for part in [p for p in remote_path.split("/") if p]:
            cur += "/" + part
            self._mkdir(cur)

    # ---- 列目录 / 移动（整理旧备份用） ----
    _UA = {"User-Agent": "pan.baidu.com"}

    def list_dir(self, remote_path: str) -> list[dict]:
        """列网盘目录，返回 [{name, isdir, path}]；目录不存在就当空。"""
        if self.mode != "api":
            return []
        self.ensure_token()
        remote_path = pan_safe_path(remote_path)
        out: list[dict] = []
        start = 0
        while True:
            try:
                r = requests.get(
                    XPAN_FILE,
                    params={
                        "method": "list",
                        "access_token": self.token,
                        "dir": remote_path,
                        "start": start,
                        "limit": 1000,
                        "order": "name",
                    },
                    headers=self._UA,
                    timeout=30,
                )
                data = r.json()
            except Exception as exc:  # noqa: BLE001
                self.log(f"  读网盘目录失败：{remote_path}（{exc}）")
                return out
            errno = data.get("errno")
            if errno in (-9, 2):          # 目录不存在
                return out
            if errno not in (0, None):
                self.log(f"  读网盘目录失败：{remote_path}（errno={errno}）")
                return out
            listing = data.get("list") or []
            for item in listing:
                out.append({
                    "name": str(item.get("server_filename") or ""),
                    "isdir": int(item.get("isdir") or 0) == 1,
                    "path": str(item.get("path") or ""),
                })
            if len(listing) < 1000:
                break
            start += 1000
        return out

    def move(self, src: str, dest_dir: str, newname: str = "") -> bool:
        """把 src（文件或整个目录）移到 dest_dir 下。"""
        if self.mode != "api":
            return False
        self.ensure_token()
        src = pan_safe_path(src)
        dest_dir = pan_safe_path(dest_dir)
        self._ensure_dir(dest_dir)
        name = pan_safe_path(newname or src.rstrip("/").split("/")[-1])
        payload = [{"path": src, "dest": dest_dir, "newname": name}]
        try:
            r = requests.post(
                XPAN_FILE,
                params={"method": "filemanager", "opera": "move", "access_token": self.token},
                data={"async": 0, "filelist": json.dumps(payload, ensure_ascii=False)},
                headers=self._UA,
                timeout=60,
            )
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            self.log(f"  网盘移动失败：{src}（{exc}）")
            return False
        if data.get("errno") not in (0, None):
            self.log(f"  网盘移动失败：{src}（errno={data.get('errno')}）")
            return False
        info = data.get("info") or []
        if info and info[0].get("errno") not in (0, None):
            self.log(f"  网盘移动失败：{src}（errno={info[0].get('errno')}）")
            return False
        return True

    def _upload_api(self, local: Path, subdir: str) -> str:
        self.ensure_token()
        root = self.remote_root()
        remote_dir = pan_safe_path(f"{root}/{subdir}") if subdir else root
        self._ensure_dir(remote_dir)
        remote_path = f"{remote_dir}/{pan_safe_path(local.name)}"
        size = local.stat().st_size
        blocks = _md5_blocks(local)

        pre = requests.post(
            XPAN_FILE,
            params={"method": "precreate", "access_token": self.token},
            data={
                "path": remote_path,
                "size": size,
                "isdir": 0,
                "autoinit": 1,
                "rtype": 3,
                "block_list": json.dumps(blocks),
            },
            timeout=60,
        ).json()

        errno = pre.get("errno")
        if errno in (-6, 111):
            if self._refresh():
                return self._upload_api(local, subdir)
            raise RuntimeError("网盘 token 失效，请重新授权")
        if errno == 318:
            self.log(f"  网盘秒传成功：{local.name}")
            return remote_path
        if errno not in (0, None):
            raise RuntimeError(f"预上传失败 errno={errno} {pre.get('errmsg') or pre}")

        uploadid = pre.get("uploadid")
        sent = [i for i in (pre.get("block_list") or [])]
        with open(local, "rb") as f:
            for idx in range(len(blocks)):
                part = f.read(BLOCK)
                if not part:
                    break
                if sent and str(idx) not in {str(s) for s in sent}:
                    continue
                resp = requests.post(
                    SUPERFILE,
                    params={
                        "method": "upload",
                        "access_token": self.token,
                        "type": "tmpfile",
                        "path": remote_path,
                        "uploadid": uploadid,
                        "partseq": idx,
                    },
                    files={"file": ("blob", part)},
                    timeout=300,
                ).json()
                if resp.get("error_code"):
                    raise RuntimeError(f"分片 {idx} 上传失败：{resp.get('error_msg') or resp}")

        fin = requests.post(
            XPAN_FILE,
            params={"method": "create", "access_token": self.token},
            data={
                "path": remote_path,
                "size": size,
                "isdir": 0,
                "rtype": 3,
                "block_list": json.dumps(blocks),
                "uploadid": uploadid,
            },
            timeout=120,
        ).json()
        if fin.get("errno") not in (0, None):
            raise RuntimeError(f"创建文件失败 errno={fin.get('errno')} {fin.get('errmsg') or fin}")
        self.log(f"  已上传网盘：{remote_path}")
        return remote_path

    def quota(self) -> dict:
        """返回 {used, total}（字节），失败返回 {}。"""
        if not self.token:
            return {}
        try:
            d = requests.get(
                "https://pan.baidu.com/api/quota",
                params={"access_token": self.token, "checkfree": 1, "checkexpire": 1},
                timeout=20,
            ).json()
            if d.get("errno") == 0:
                return {"used": d.get("used"), "total": d.get("total")}
        except Exception:  # noqa: BLE001
            pass
        return {}

    # ---------------- 统一入口 ----------------
    def save(self, local: Path, subdir: str = "") -> str:
        if self.mode == "api":
            return self._upload_api(local, subdir)
        return self._save_to_sync_dir(local, subdir)
