"""Adapter for preview runners that append Vite-style command-line flags."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class PreviewHandler(server.Handler):
    def do_GET(self):
        if self.path == "/_mobile_qa":
            return self.respond(200, """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>手机尺寸双端检查</title><style>
body{margin:0;background:#e7eae1;font-family:system-ui;display:flex;justify-content:center;gap:36px;padding:24px 0}
.phone{width:390px;flex-shrink:0}p{margin:0 0 10px;font-size:13px;color:#526c5b}iframe{width:390px;height:844px;border:1px solid #d7dece;border-radius:24px;background:#f7f5ed;box-shadow:0 14px 45px #244c3617}
</style><div class="phone"><p>前端 · 390 × 844</p><iframe title="前端" src="/#front"></iframe></div><div class="phone"><p>后端 · 390 × 844</p><iframe title="后端" src="/#back"></iframe></div></html>""".encode(), "text/html; charset=utf-8")
        return super().do_GET()


server.Handler = PreviewHandler

sys.argv = [sys.argv[0], "--db", ".preview-data/orders.sqlite3"] + [
    item for item in sys.argv[1:] if item != "--strictPort"
]
raise SystemExit(server.main())
