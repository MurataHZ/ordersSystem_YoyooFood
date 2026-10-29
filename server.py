#!/usr/bin/env python3
"""Two-phone stall order board. Python 3.9+, standard library only."""
import argparse
import copy
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import sys
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
FLAVORS = {"mango", "strawberry", "passion"}
PEARLS = {"strawberry", "apple", "blueberry", "mango", "watermelon", "none"}
PRICES = {"small": 599, "large": 699}
ACCOUNTING_PRICES = {"small": 600, "large": 700}


class OrderError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def empty_state():
    return {"schemaVersion": 3, "revision": 0, "nextNumber": 1,
            "queue": [], "completed": [], "archived": {"count": 0, "cents": 0},
            "archiveHistory": [], "undo": []}


def migrate_state(state):
    """Preserve totals and orders; never invent dates missing from old records."""
    version = state["schemaVersion"]
    if version == 3:
        return False
    if version not in (1, 2):
        raise ValueError("不支持此订单数据版本，请使用匹配版本的程序。")
    if version == 1:
        for order in state["queue"] + state["completed"] + [e["order"] for e in state["undo"]]:
            order["accountingCents"] = ACCOUNTING_PRICES[order["size"]]
        # V1 charged 599/699 cents. Both become exactly one cent more per cup.
        state["archived"]["cents"] += state["archived"]["count"]
    archived = state["archived"]
    state["archiveHistory"] = ([{"id": "legacy", "date": None, "count": archived["count"],
                                 "cents": archived["cents"], "archivedAt": None, "legacy": True}]
                               if archived["count"] or archived["cents"] else [])
    state["schemaVersion"] = 3
    state["revision"] += 1
    return True


@lru_cache(maxsize=1)
def order_timezone():
    # Bundled TZif data works with embeddable Python on Windows, without pip.
    with (ROOT / "timezones" / "Pacific_Auckland").open("rb") as source:
        return ZoneInfo.from_file(source, key="Pacific/Auckland")


def order_date(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(order_timezone()).date().isoformat()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def apply_action(state, action, payload):
    """Mutates a transaction-local state; money is always integer cents."""
    if not isinstance(payload, dict):
        raise OrderError("订单参数格式不正确。")
    if action == "create":
        flavors = payload.get("flavors")
        pearl, size = payload.get("pearl"), payload.get("size")
        if (not isinstance(flavors, list) or len(flavors) != 2
                or not all(isinstance(f, str) and f in FLAVORS for f in flavors)
                or not isinstance(pearl, str) or pearl not in PEARLS
                or not isinstance(size, str) or size not in PRICES):
            raise OrderError("请选择两种口味、一种珍珠和 Size。")
        order = {"id": str(uuid.uuid4()), "number": state["nextNumber"],
                 "flavors": flavors[:], "pearl": pearl, "size": size,
                 "priceCents": PRICES[size], "accountingCents": ACCOUNTING_PRICES[size],
                 "createdAt": now()}
        state["nextNumber"] += 1
        state["queue"].append(order)
    elif action == "complete":
        if not state["queue"]:
            raise OrderError("当前没有待完成的订单。", 409)
        if state["queue"][0]["id"] != payload.get("id"):
            raise OrderError("队首订单已发生变化，请查看最新订单后再操作。", 409)
        order = state["queue"].pop(0)
        state["undo"].append({"kind": "complete", "order": copy.deepcopy(order)})
        order["completedAt"] = now()
        state["completed"].append(order)
    elif action in {"delete", "sales_delete"}:
        target = state["queue"] if action == "delete" else state["completed"]
        index = next((i for i, o in enumerate(target) if o["id"] == payload.get("id")), None)
        if index is None:
            raise OrderError("该订单已发生变化，请查看最新列表。", 409)
        order = target.pop(index)
        if action == "delete":
            state["undo"].append({"kind": "delete", "order": copy.deepcopy(order)})
        else:
            state["undo"] = [e for e in state["undo"] if e["order"]["id"] != order["id"]]
    elif action == "undo":
        if not state["undo"]:
            raise OrderError("没有可以撤销的删除或归档。", 409)
        event = state["undo"].pop()
        order = event["order"]
        state["completed"] = [o for o in state["completed"] if o["id"] != order["id"]]
        if any(o["id"] == order["id"] for o in state["queue"]):
            raise OrderError("该订单已经在队列中。", 409)
        order.pop("completedAt", None)
        state["queue"].insert(0, order)
    elif action == "archive":
        ids = payload.get("ids")
        actual = [o["id"] for o in state["completed"]]
        if not actual:
            raise OrderError("当前没有可以永久归档的订单。", 409)
        if not isinstance(ids, list) or ids != actual:
            raise OrderError("完成订单列表已更新，请核对最新金额后重新归档。", 409)
        count = len(actual)
        cents = sum(o["accountingCents"] for o in state["completed"])
        state["archiveHistory"].append({"id": str(uuid.uuid4()),
                                       "date": order_date(state["completed"][0]["createdAt"]),
                                       "count": count, "cents": cents,
                                       "archivedAt": now(), "legacy": False})
        state["archived"]["count"] += count
        state["archived"]["cents"] += cents
        state["completed"] = []
        id_set = set(actual)
        state["undo"] = [e for e in state["undo"] if e["order"]["id"] not in id_set]
    else:
        raise OrderError("未知操作。")
    state["revision"] += 1


class Store:
    """SQLite serializes mutations; receipts make network retries idempotent."""
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO state VALUES (1, ?)", (json.dumps(empty_state()),))
            db.execute("BEGIN IMMEDIATE")
            try:
                state = json.loads(db.execute("SELECT data FROM state WHERE id=1").fetchone()[0])
                if migrate_state(state):
                    db.execute("UPDATE state SET data=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def read(self):
        with self.connection() as db:
            return json.loads(db.execute("SELECT data FROM state WHERE id=1").fetchone()[0])

    def command(self, request):
        if not isinstance(request, dict):
            raise OrderError("请求格式不正确。")
        request_id = request.get("requestId")
        action = request.get("action")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100 or not isinstance(action, str):
            raise OrderError("操作编号不正确。")
        payload = request.get("payload", {})
        fingerprint = hashlib.sha256(json.dumps([action, payload], sort_keys=True).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                receipt = db.execute("SELECT fingerprint FROM commands WHERE id=?", (request_id,)).fetchone()
                state = json.loads(db.execute("SELECT data FROM state WHERE id=1").fetchone()[0])
                if receipt:
                    if receipt[0] != fingerprint:
                        raise OrderError("同一操作编号不能用于不同操作。", 409)
                else:
                    apply_action(state, action, payload)
                    db.execute("UPDATE state SET data=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))
                    db.execute("INSERT INTO commands VALUES (?, ?)", (request_id, fingerprint))
                db.execute("COMMIT")
                return state
            except BaseException:
                db.execute("ROLLBACK")
                raise


class Handler(BaseHTTPRequestHandler):
    server_version = "BobaOrders/1.0"

    def respond(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def routed_path(self):
        """Remote mode requires an unguessable path for pages AND API access."""
        path = urlsplit(self.path).path
        prefix = getattr(self.server, "access_prefix", "")
        if prefix:
            expected = prefix + "/"
            candidate = path[:len(expected)]
            if not hmac.compare_digest(candidate.encode(), expected.encode()):
                return None
            return "/" + path[len(expected):]
        return path

    def do_GET(self):
        path = self.routed_path()
        if path == "/api/state":
            try:
                self.respond(200, self.server.store.read())
            except sqlite3.Error:
                self.respond(503, {"error": "暂时无法读取订单，请稍后重试。"})
        elif path in {"/", "/index.html"}:
            html = (ROOT / "index.html").read_text(encoding="utf-8")
            prefix = getattr(self.server, "access_prefix", "")
            if prefix:
                html = html.replace('<html lang="zh-CN">',
                                    '<html lang="zh-CN" data-api-base="' + prefix + '">', 1)
            self.respond(200, html.encode(), "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            self.respond(204, b"", "image/x-icon")
        else:
            self.respond(404, {"error": "找不到这个页面。"})

    def do_POST(self):
        if self.routed_path() != "/api/commands":
            return self.respond(404, {"error": "找不到这个接口。"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json" or self.headers.get("X-Boba-Client") != "1":
            return self.respond(415, {"error": "请求格式不正确。"})
        # No CORS is enabled. JSON + this custom header prevent cross-site form writes.
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise OrderError("请求内容过大或为空。", 413)
            self.connection.settimeout(10)
            request = json.loads(self.rfile.read(length))
            self.respond(200, self.server.store.command(request))
        except OrderError as error:
            self.respond(error.status, {"error": str(error)})
        except (ValueError, UnicodeError):
            self.respond(400, {"error": "请求内容无法读取。"})
        except (sqlite3.Error, OSError):
            self.respond(503, {"error": "暂时无法保存，请重新连接后重试。"})

    def log_message(self, fmt, *args):
        if getattr(self.server, "access_prefix", ""):
            return  # Never write the private link or its API paths into access logs.
        if not self.path.startswith("/api/state"):
            super().log_message(fmt, *args)


def addresses():
    found = set()
    try:
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            found.add(sock.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in found if not ip.startswith("127.") and ip != "0.0.0.0")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Yoyoo Food Station 订单系统（无需第三方依赖）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--db", default=str(ROOT / "data" / "orders.sqlite3"))
    parser.add_argument("--open", action="store_true", help="自动打开电脑浏览器")
    args = parser.parse_args()
    store = Store(args.db)
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as error:
        print("启动失败：", error)
        print("如果端口已被占用，可以运行：python server.py --port 8081")
        return 1
    server.store = store
    print("\nYoyoo Food Station 订单系统已启动。请保持此窗口打开。")
    print(f"电脑访问：http://127.0.0.1:{args.port}")
    for ip in addresses():
        print(f"手机访问：http://{ip}:{args.port}")
    print("两台手机与此电脑连接同一个 Wi-Fi；不需要外网。")
    print("订单自动保存。按 Ctrl+C 停止服务。\n", flush=True)
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n服务已停止，订单已保留。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
