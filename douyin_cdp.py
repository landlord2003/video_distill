#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douyin_cdp.py — CDP 真实浏览器兜底层（借鉴 MediaCrawler 的 connect_over_cdp 思路）

背景：无头（headless）会话在高频探测后会被抖音风控「可见性降级」——
接口返回 200 但列表/详情为空，重导 cookie 无效，唯一不靠等冷却的出路
就是真实浏览器形态（真实指纹 + 有头窗口 + 持久登录态）。

本模块维护一个「常驻调试浏览器」：
  - 真实 Edge/Chrome 二进制 + 有头窗口（指纹与真人浏览一致）
  - 独立持久 profile（cdp_profile/），首次连接自动导入项目 cookies.txt 登录态，
    之后登录态由浏览器自己维护（比静态 cookie 更稳）
  - 通过 --remote-debugging-port 暴露 CDP，playwright connect_over_cdp 接管页面

上层用法（douyin_auto / douyin_profile / douyin_comments）：
  无头失败且疑似风控降级时，connect(p) 拿到浏览器，
  在 default_context 上开新页重跑同一套拦截逻辑。浏览器不关闭，跨请求复用。

环境变量：DY_CDP_PORT（默认 9222）、DY_CDP_BROWSER（指定浏览器 exe 绝对路径）
"""
import os
import time
import json
import shutil
import subprocess
import urllib.request

PROJ = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(PROJ, "cdp_profile")
_COOKIE_MARKER = os.path.join(PROFILE_DIR, "_cookies_imported")

# 常见浏览器安装位置（Windows）
_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _port():
    try:
        return int(os.environ.get("DY_CDP_PORT") or 9222)
    except ValueError:
        return 9222


def _endpoint():
    return f"http://127.0.0.1:{_port()}"


def _alive(verbose=False):
    """调试端口上是否已有活浏览器（/json/version 可达）。"""
    try:
        with urllib.request.urlopen(_endpoint() + "/json/version", timeout=2) as r:
            info = json.loads(r.read().decode("utf-8", "ignore"))
        if verbose:
            print(f"[cdp] 端口 {_port()} 已有浏览器: {info.get('Browser', '?')[:60]}")
        return True
    except Exception:
        return False


def _find_browser_exe():
    """定位本机 Edge/Chrome：环境变量 DY_CDP_BROWSER 优先，其次常见安装路径。"""
    env = (os.environ.get("DY_CDP_BROWSER") or "").strip()
    if env and os.path.isfile(env):
        return env
    for cand in _BROWSER_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def ensure_browser(verbose=True, wait_secs=25):
    """确保调试浏览器在跑：已在则直接用；否则拉起真实 Edge/Chrome（有头+独立 profile）。"""
    if _alive(verbose):
        return True
    exe = _find_browser_exe()
    if not exe:
        if verbose:
            print("[cdp] 未找到本机 Edge/Chrome，CDP 兜底不可用")
        return False
    os.makedirs(PROFILE_DIR, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={_port()}",
        # 独立 profile：避免与用户日常浏览器进程冲突（同 exe 已在跑时
        # 不带独立 profile 的启动只会并入现有进程、调试端口不会生效）
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=msEdgeUsePictureInPictureOnTabHover",
        "--start-maximized",
        "about:blank",
    ]
    creationflags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
    try:
        subprocess.Popen(args, cwd=os.path.dirname(exe),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=creationflags)
    except Exception as e:
        if verbose:
            print(f"[cdp] 启动浏览器失败: {e}")
        return False
    for _ in range(wait_secs):
        if _alive():
            if verbose:
                print(f"[cdp] 调试浏览器已就绪（{_endpoint()}，profile={PROFILE_DIR}）")
            return True
        time.sleep(1)
    if verbose:
        print("[cdp] 浏览器启动后调试端口未就绪（超时）")
    return False


def connect(p, verbose=True):
    """playwright 接管调试浏览器。返回 browser 对象；失败抛 RuntimeError。
    p: 已进入的 sync_playwright() 上下文实例。
    """
    if not ensure_browser(verbose=verbose):
        raise RuntimeError("CDP 调试浏览器不可用（未找到 Edge/Chrome 或启动失败）")
    try:
        browser = p.chromium.connect_over_cdp(_endpoint(), timeout=15000)
    except Exception as e:
        raise RuntimeError(f"CDP 连接失败: {e}")
    _import_cookies_once(browser, verbose=verbose)
    return browser


def default_context(browser):
    """CDP 浏览器的默认上下文（持久登录态所在）。没有就建一个。"""
    if browser.contexts:
        return browser.contexts[0]
    return browser.new_context()


def new_page(context):
    """在指定上下文开新页（带反检测 init script）。"""
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
    return context.new_page()


def _import_cookies_once(browser, verbose=True):
    """首次使用时把项目 cookies.txt 登录态导入真实浏览器（此后由浏览器自行维护）。"""
    if os.path.isfile(_COOKIE_MARKER):
        return
    try:
        from douyin_auto import resolve_cookie_file, parse_netscape
        cf = resolve_cookie_file()
        ctx = default_context(browser)
        imported = 0
        if cf:
            cookies = parse_netscape(cf)
            if cookies:
                ctx.add_cookies(cookies)
                imported = len(cookies)
        with open(_COOKIE_MARKER, "w", encoding="utf-8") as f:
            f.write(f"{time.time()}\n{imported}\n")
        if verbose:
            print(f"[cdp] 首次初始化：导入 cookie {imported} 个（来源: {cf or '无'}）")
    except Exception as e:
        # 导入失败不阻塞主流程：真实浏览器用户可手动登录一次抖音
        if verbose:
            print(f"[cdp] cookie 导入失败(可手动在调试浏览器里登录抖音): {e}")


def stop(verbose=True):
    """关闭常驻调试浏览器（按端口找 PID，温和结束）。"""
    try:
        if not _alive():
            return True
        # 通过 CDP 的 json/version 拿不到 PID，改用 netstat 等价：Windows 下用 PowerShell
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-NetTCPConnection -LocalPort {_port()} -State Listen "
                 f"| Select-Object -First 1).OwningProcess"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15).stdout.strip()
            pid = int(out) if out.isdigit() else 0
            if pid:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=15)
                if verbose:
                    print(f"[cdp] 调试浏览器已停止 (pid={pid})")
                return True
        return False
    except Exception as e:
        if verbose:
            print(f"[cdp] 停止失败: {e}")
        return False


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        stop()
    else:
        ok = ensure_browser(verbose=True)
        print("CDP 浏览器:", "就绪" if ok else "不可用")
