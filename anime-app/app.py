#!/usr/bin/env python3
"""超越极限 - 桌面入口 (pythonw 无窗口运行)
启动 HTTP 服务 -> 打开 Edge/Chrome 独立窗口 -> 窗口关闭后自动退出
"""
import os, sys, subprocess, threading, time

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8965


def _open_app_window():
    """用 Edge/Chrome --app 模式打开独立窗口"""
    url = "http://127.0.0.1:%d/" % PORT
    cands = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    browser = next((p for p in cands if p and os.path.isfile(p)), None)
    if not browser:
        import webbrowser
        webbrowser.open(url)
        return None

    app_data = os.path.join(ROOT, ".app-data")
    proc = subprocess.Popen([
        browser,
        "--app=%s?app=1" % url,
        "--user-data-dir=%s" % app_data,
        "--window-size=1380,880",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--disk-cache-size=268435456",
        "--enable-features=WinFramelessWindowControls,WebFloatingUIVar,WebUIRoundedCorners",
        "--disable-features=msWebOOUI",
    ])
    return proc


def main():
    sys.path.insert(0, ROOT)
    import server
    server.PORT = PORT

    # 后台启动 HTTP 服务 (复用 server.main 的逻辑, 但在子线程中运行)
    def _serve():
        server.main()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    # 等服务就绪
    for _ in range(40):
        time.sleep(0.25)
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:%d/api/health" % PORT, timeout=1)
            break
        except Exception:
            pass

    # 打开独立窗口, 窗口关闭后退出
    proc = _open_app_window()
    if proc:
        proc.wait()
    else:
        t.join()


if __name__ == "__main__":
    main()
