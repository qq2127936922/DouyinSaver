# 测试脚本

这些是开发期的验证脚本，不是自动化单元测试，多数需要**联网 + 已登录的抖音账号**，
跑起来会真的访问抖音、下载少量作品，请按需手动执行。

```bash
.venv/Scripts/python.exe tests/test_url_refresh.py      # 离线，不联网（用假地址模拟 403）
```

## 清单

| 脚本 | 联网 | 说明 |
|---|---|---|
| `test_url_refresh.py` | 否 | 签名地址 403 过期 → 重抓详情换新地址的逻辑；不依赖网络，`--selftest` 之外唯一能随时跑的 |
| `test_live_photo.py` | 是 | 实况/动态图识别 + 合成（可传链接：`python tests/test_live_photo.py "https://v.douyin.com/xxx/"`） |
| `test_collect.py` | 是 | 收藏夹列表抓取 |
| `test_collect_upload.py` | 是 | 收藏夹作品落盘 + 传网盘 |
| `test_link_upload.py` | 是 | 单条链接下载 + 传网盘 |
| `test_sync_folder.py` | 是 | 同步文件夹模式上传 |
| `test_both_real.py` | 是 | 「收藏 + 点赞」交集范围 |
| `test_scope_intersection.py` | 是 | 交集过滤的纯逻辑校验 |
| `test_layout.py` | 是 | 两种落盘布局（收藏板块 / 视频板块） |
| `test_reclassify.py` | 是 | 旧备份按收藏夹归位 |
| `test_video_bucket.py` | 是 | 没有收藏夹的作品进「视频」 |
| `test_two_modules.py` | 是 | 两个板块同时跑 |
| `test_upload_route.py` | 是 | 上传目录路由 |

## 登录态

联网脚本共用 `.probe_browser/`（项目根下的 Playwright 持久化目录）。
第一次跑如果提示未登录，用带界面的方式跑一次并扫码登录：

```bash
.venv/Scripts/python.exe cli.py --show-browser "随便一个抖音链接"
```

关掉窗口后登录态就留在 `.probe_browser/` 里，后续脚本复用。
这个目录含账号 Cookie，已在 `.gitignore` 里排除，不会进仓库。
