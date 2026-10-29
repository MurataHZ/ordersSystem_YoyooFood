#!/usr/bin/env python3
"""Private-link remote entry point. No Cloudflare account or router changes."""
import argparse
import ctypes
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))  # Also works with Windows embeddable Python.
from server import Handler, Store, ThreadingHTTPServer

URL_PATTERN = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com(?=$|[\s/|"<>])')


def extract_tunnel_url(line):
    match = URL_PATTERN.search(line)
    return match.group(0) if match else None


def private_url(origin, prefix):
    return origin.rstrip("/") + prefix + "/"


def cloudflare_command(executable, port):
    return [str(executable), "tunnel", "--no-autoupdate", "--protocol", "http2",
            "--url", f"http://127.0.0.1:{port}"]


def keep_awake(enabled):
    if os.name == "nt":
        try:
            # Only for this process's lifetime; do not change Windows settings.
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if enabled else 0x80000000)
        except (AttributeError, OSError):
            pass


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Yoyoo Food Station 远程访问启动器")
    parser.add_argument("--cloudflared", default=str(ROOT / "runtime" / "cloudflared.exe"))
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    executable = Path(args.cloudflared)
    if not executable.is_file():
        print("还没有准备远程工具。请双击 start-remote-windows.cmd。")
        return 1

    store = Store(ROOT / "data" / "orders.sqlite3")
    try:
        # Bind only to loopback. The tunnel is the only remote entry point.
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"端口 {args.port} 被占用。请先关闭旧的订单服务器窗口，再重新启动。")
        return 1
    httpd.store = store
    httpd.access_prefix = "/s/" + secrets.token_urlsafe(24)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    events = queue.Queue()
    process = None
    keep_awake(True)
    print("\nYoyoo Food Station · 远程版")
    print("订单服务器已启动，正在申请 HTTPS 手机访问地址……", flush=True)

    try:
        process = subprocess.Popen(
            cloudflare_command(executable, args.port), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )

        def read_output():
            for line in process.stdout:
                events.put(line.rstrip())
            events.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + 90
        hostname = None
        connected = False
        announced = False
        recent = []

        while True:
            try:
                line = events.get(timeout=1)
            except queue.Empty:
                line = ""
            if line is None or process.poll() is not None:
                print("\n远程连接进程已停止。订单仍保存在 data 文件夹中。")
                if not announced:
                    for message in recent[-8:]:
                        print(message)
                print("请检查电脑网络，再关闭本窗口并重新启动；重新启动后使用新的手机链接。")
                return 1
            if line:
                recent.append(line)
                recent = recent[-12:]
                hostname = hostname or extract_tunnel_url(line)
                if "Registered tunnel connection" in line:
                    connected = True
            if not announced and hostname and connected:
                link = private_url(hostname, httpd.access_prefix)
                print("\n================ 远程连接已建立 ================")
                print("请把下面的完整链接发送到两台手机，用 Safari 打开：\n")
                print(link)
                print("\n================================================")
                print("一台选择前端，另一台选择后端。手机可以用流量或其他 Wi-Fi。")
                print("完整链接含随机访问码，只分享给负责接单和制作的人。")
                print("请保持电脑联网和此窗口打开。可以锁屏或关闭显示器，不要关机。")
                print("启动器运行时会尝试阻止自动睡眠。按 Ctrl+C 停止。")
                print("每次重新启动，手机链接会变化；已有订单保留。\n", flush=True)
                webbrowser.open(link)
                announced = True
            if not announced and time.monotonic() > deadline:
                print("\n等待远程连接超时。请检查电脑是否能正常上网，以及能否访问 Cloudflare。")
                for message in recent[-8:]:
                    print(message)
                return 1
    except KeyboardInterrupt:
        print("\n正在停止，订单会继续保存在 data 文件夹中。")
        return 0
    except OSError as error:
        print("启动远程连接失败：", error)
        return 1
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=3)
        keep_awake(False)


if __name__ == "__main__":
    raise SystemExit(main())
