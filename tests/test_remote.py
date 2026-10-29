"""Private-link routing and startup orchestration; never opens a public tunnel."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Handler, Store, ThreadingHTTPServer
import remote_launcher


class PrivateLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.store = Store(Path(cls.temp.name) / "orders.sqlite3")
        cls.server.access_prefix = "/s/test-only-access-code"
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"
        cls.base = cls.origin + cls.server.access_prefix
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def test_missing_wrong_or_partial_link_cannot_read_or_write(self):
        for path in ["/", "/api/state", "/s/wrong/api/state",
                     self.server.access_prefix + "wrong/api/state"]:
            with self.assertRaises(HTTPError) as error:
                urlopen(self.origin + path)
            self.assertEqual(error.exception.code, 404)
        for path in ["/api/commands", "/s/wrong/api/commands"]:
            req = Request(self.origin + path, data=b'{}',
                          headers={"Content-Type": "application/json", "X-Boba-Client": "1"})
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            self.assertEqual(error.exception.code, 404)

    def test_page_uses_scoped_api_and_hides_database(self):
        with urlopen(self.base + "/") as response:
            html = response.read().decode()
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertIn('data-api-base="/s/test-only-access-code"', html)
        for path in ["/server.py", "/data/orders.sqlite3", "/../server.py"]:
            with self.assertRaises(HTTPError) as error:
                urlopen(self.base + path)
            self.assertEqual(error.exception.code, 404)

    def test_two_phones_share_orders_through_private_api(self):
        payload = {"requestId": str(uuid.uuid4()), "action": "create",
                   "payload": {"flavors": ["mango", "passion"], "pearl": "apple", "size": "large"}}
        req = Request(self.base + "/api/commands", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "X-Boba-Client": "1"})
        with urlopen(req) as response:
            created = json.load(response)
        with urlopen(self.base + "/api/state") as response:
            shared = json.load(response)
        self.assertEqual(created, shared)
        self.assertEqual(shared["queue"][-1]["priceCents"], 699)
        self.assertEqual(shared["queue"][-1]["accountingCents"], 700)


class LauncherTests(unittest.TestCase):
    def test_only_cloudflare_hostname_is_extracted(self):
        self.assertEqual(remote_launcher.extract_tunnel_url(
            "INFO | https://stall-demo.trycloudflare.com |"), "https://stall-demo.trycloudflare.com")
        for line in ["https://unrelated.example", "https://a.trycloudflare.com.evil.example",
                     "http://a.trycloudflare.com", "INFO no address yet"]:
            self.assertIsNone(remote_launcher.extract_tunnel_url(line))

    def test_full_link_keeps_access_code_and_trailing_slash(self):
        self.assertEqual(remote_launcher.private_url("https://a.trycloudflare.com/", "/s/code"),
                         "https://a.trycloudflare.com/s/code/")
        command = remote_launcher.cloudflare_command(Path("path with spaces/cloudflared.exe"), 8080)
        self.assertEqual(command[0], "path with spaces/cloudflared.exe")
        self.assertIn("http://127.0.0.1:8080", command)
        self.assertNotIn("/s/code", command)

    def test_launcher_announces_only_after_connection_and_cleans_up(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = io.StringIO("INFO https://sample.trycloudflare.com\n"
                                          "INFO Registered tunnel connection\n")
                self.stopped = False
            def poll(self):
                return 0 if self.stopped else None
            def terminate(self):
                self.stopped = True
            def wait(self, timeout=None):
                return 0
        class FakeServer:
            def __init__(self, *args):
                self.closed = False
            def serve_forever(self):
                pass
            def shutdown(self):
                pass
            def server_close(self):
                self.closed = True
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "cloudflared.exe"
            executable.write_bytes(b"dummy")
            process, fake_server = FakeProcess(), FakeServer()
            captured = io.StringIO()
            with patch.object(remote_launcher, "ROOT", root), \
                 patch.object(remote_launcher.sys, "argv", ["launcher", "--cloudflared", str(executable)]), \
                 patch.object(remote_launcher, "ThreadingHTTPServer", return_value=fake_server), \
                 patch.object(remote_launcher.subprocess, "Popen", return_value=process), \
                 patch.object(remote_launcher.secrets, "token_urlsafe", return_value="fake-test-code"), \
                 patch.object(remote_launcher.webbrowser, "open") as opened, \
                 contextlib.redirect_stdout(captured):
                remote_launcher.main()
            opened.assert_called_once_with("https://sample.trycloudflare.com/s/fake-test-code/")
            self.assertIn("远程连接已建立", captured.getvalue())
            self.assertTrue(process.stopped)
            self.assertTrue(fake_server.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
