"""Business and HTTP integration tests. No third-party packages required."""
import concurrent.futures
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
from server import Handler, OrderError, Store, ThreadingHTTPServer, order_date


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "orders.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def command(self, action, payload=None, request_id=None):
        return self.store.command({"requestId": request_id or str(uuid.uuid4()),
                                   "action": action, "payload": payload or {}})

    def create(self, size="small"):
        state = self.command("create", {"flavors": ["mango", "mango"],
                                        "pearl": "apple", "size": size,
                                        "priceCents": 1, "accountingCents": 1})
        return state["queue"][-1]

    def test_price_duplicate_flavors_and_fifo(self):
        first, second = self.create(), self.create("large")
        self.assertEqual(first["flavors"], ["mango", "mango"])
        self.assertEqual(first["priceCents"], 599)
        self.assertEqual(second["priceCents"], 699)
        self.assertEqual(first["accountingCents"], 600)
        self.assertEqual(second["accountingCents"], 700)
        state = self.command("complete", {"id": first["id"]})
        self.assertEqual([o["id"] for o in state["queue"]], [second["id"]])
        self.assertEqual(state["completed"][0]["id"], first["id"])

    def test_undo_restores_to_front_and_removes_sales(self):
        first, second, third = self.create(), self.create("large"), self.create()
        self.command("complete", {"id": first["id"]})
        self.command("delete", {"id": third["id"]})
        state = self.command("undo")
        self.assertEqual([o["id"] for o in state["queue"]], [third["id"], second["id"]])
        state = self.command("undo")
        self.assertEqual(state["queue"][0]["id"], first["id"])
        self.assertEqual(state["completed"], [])

    def test_sales_delete_is_permanent(self):
        first, second = self.create(), self.create("large")
        self.command("complete", {"id": first["id"]})
        self.command("complete", {"id": second["id"]})
        state = self.command("sales_delete", {"id": first["id"]})
        self.assertEqual(sum(o["accountingCents"] for o in state["completed"]), 700)
        state = self.command("undo")
        self.assertEqual(state["queue"][0]["id"], second["id"])
        with self.assertRaises(OrderError):
            self.command("undo")

    def test_archive_accumulates_and_cannot_be_undone(self):
        for size in ["small", "large"]:
            order = self.create(size)
            self.command("complete", {"id": order["id"]})
        ids = [o["id"] for o in self.store.read()["completed"]]
        state = self.command("archive", {"ids": ids})
        self.assertEqual(state["archived"], {"count": 2, "cents": 1300})
        self.assertEqual(state["completed"], [])
        with self.assertRaises(OrderError):
            self.command("undo")
        order = self.create()
        self.command("complete", {"id": order["id"]})
        state = self.command("archive", {"ids": [order["id"]]})
        self.assertEqual(state["archived"], {"count": 3, "cents": 1900})

    def test_legacy_bookkeeping_migrates_once_and_preserves_display_prices(self):
        first, second = self.create(), self.create("large")
        self.command("complete", {"id": first["id"]})
        legacy = self.store.read()
        legacy["schemaVersion"] = 1
        legacy["archived"] = {"count": 3, "cents": 1897}
        for order in legacy["queue"] + legacy["completed"] + [e["order"] for e in legacy["undo"]]:
            del order["accountingCents"]
        with self.store.connection() as db:
            db.execute("UPDATE state SET data=? WHERE id=1", (json.dumps(legacy),))
        self.store = Store(self.path)
        migrated = self.store.read()
        self.assertEqual(migrated["schemaVersion"], 3)
        self.assertEqual(migrated["archived"], {"count": 3, "cents": 1900})
        self.assertEqual(migrated["archiveHistory"][0]["cents"], 1900)
        self.assertIsNone(migrated["archiveHistory"][0]["date"])
        self.assertEqual(migrated["queue"][0]["priceCents"], 699)
        self.assertEqual(migrated["queue"][0]["accountingCents"], 700)
        self.assertEqual(migrated["completed"][0]["priceCents"], 599)
        self.assertEqual(migrated["completed"][0]["accountingCents"], 600)
        self.assertEqual(migrated["undo"][0]["order"]["accountingCents"], 600)
        self.store = Store(self.path)
        self.assertEqual(self.store.read(), migrated)
        state = self.command("undo")
        self.assertEqual(state["queue"][0]["id"], first["id"])
        self.assertEqual(state["queue"][1]["id"], second["id"])

    def test_none_pearl_keeps_the_same_size_prices(self):
        state = self.command("create", {"flavors": ["mango", "passion"],
                                        "pearl": "none", "size": "small"})
        order = state["queue"][0]
        self.assertEqual(order["pearl"], "none")
        self.assertEqual(order["priceCents"], 599)
        self.assertEqual(order["accountingCents"], 600)
        self.command("complete", {"id": order["id"]})
        state = self.command("archive", {"ids": [order["id"]]})
        self.assertEqual(state["archiveHistory"][0]["count"], 1)
        self.assertEqual(state["archiveHistory"][0]["cents"], 600)

    def test_history_uses_first_listed_order_nz_date_instead_of_archive_date(self):
        first, second = self.create(), self.create("large")
        self.command("complete", {"id": first["id"]})
        self.command("complete", {"id": second["id"]})
        current = self.store.read()
        # The first listed order is NOT the oldest date. The list order wins.
        current["completed"][0]["createdAt"] = "2026-10-24T11:30:00Z"
        current["completed"][1]["createdAt"] = "2026-10-23T11:30:00Z"
        with self.store.connection() as db:
            db.execute("UPDATE state SET data=? WHERE id=1", (json.dumps(current),))
        with patch("server.now", return_value="2026-10-30T01:00:00Z"):
            state = self.command("archive", {"ids": [first["id"], second["id"]]})
        entry = state["archiveHistory"][0]
        self.assertEqual(entry["date"], "2026-10-25")
        self.assertEqual(entry["archivedAt"], "2026-10-30T01:00:00Z")
        self.assertEqual((entry["count"], entry["cents"]), (2, 1300))
        self.assertEqual(state["completed"], [])
        self.assertEqual(Store(self.path).read(), state)

    def test_each_archive_has_a_log_and_retries_do_not_duplicate_it(self):
        with patch("server.now", return_value="2026-10-24T20:00:00Z"):
            first = self.create()
            self.command("complete", {"id": first["id"]})
            request_id = str(uuid.uuid4())
            first_result = self.command("archive", {"ids": [first["id"]]}, request_id)
            replay = self.command("archive", {"ids": [first["id"]]}, request_id)
            self.assertEqual(first_result, replay)
            second = self.create("large")
            self.command("complete", {"id": second["id"]})
            state = self.command("archive", {"ids": [second["id"]]})
        logs = state["archiveHistory"]
        self.assertEqual(len(logs), 2)
        self.assertNotEqual(logs[0]["id"], logs[1]["id"])
        self.assertEqual([log["date"] for log in logs], ["2026-10-25", "2026-10-25"])
        self.assertEqual(sum(log["count"] for log in logs), state["archived"]["count"])
        self.assertEqual(sum(log["cents"] for log in logs), state["archived"]["cents"])

    def test_morning_v2_data_preserves_orders_totals_and_unknown_legacy_date(self):
        first, second = self.create(), self.create("large")
        self.command("complete", {"id": first["id"]})
        legacy = self.store.read()
        legacy["schemaVersion"] = 2
        legacy["archived"] = {"count": 2, "cents": 1300}
        del legacy["archiveHistory"]
        with self.store.connection() as db:
            db.execute("UPDATE state SET data=? WHERE id=1", (json.dumps(legacy),))
        self.store = Store(self.path)
        state = self.store.read()
        self.assertEqual(state["archived"], {"count": 2, "cents": 1300})
        self.assertEqual(state["queue"], legacy["queue"])
        self.assertEqual(state["completed"], legacy["completed"])
        self.assertEqual(state["undo"], legacy["undo"])
        self.assertEqual(len(state["archiveHistory"]), 1)
        self.assertIsNone(state["archiveHistory"][0]["date"])
        self.assertTrue(state["archiveHistory"][0]["legacy"])
        self.assertEqual(Store(self.path).read(), state)
        state = self.command("archive", {"ids": [first["id"]]})
        self.assertEqual(state["archived"], {"count": 3, "cents": 1900})
        self.assertEqual([log["cents"] for log in state["archiveHistory"]], [1300, 600])
        self.assertEqual(state["queue"][0]["id"], second["id"])

    def test_bundled_nz_timezone_handles_standard_and_daylight_time(self):
        self.assertEqual(order_date("2026-01-01T11:30:00Z"), "2026-01-02")
        self.assertEqual(order_date("2026-07-01T11:30:00Z"), "2026-07-01")

    def test_stale_actions_do_not_change_ledger(self):
        first, second = self.create(), self.create()
        self.command("complete", {"id": first["id"]})
        before = self.store.read()
        with self.assertRaises(OrderError):
            self.command("complete", {"id": first["id"]})
        self.assertEqual(self.store.read(), before)
        self.command("complete", {"id": second["id"]})
        before = self.store.read()
        with self.assertRaises(OrderError):
            self.command("archive", {"ids": [first["id"]]})
        self.assertEqual(self.store.read(), before)

    def test_network_retries_are_idempotent_across_restart(self):
        request_id = str(uuid.uuid4())
        payload = {"flavors": ["passion", "strawberry"], "pearl": "blueberry", "size": "large"}
        one = self.command("create", payload, request_id)
        self.store = Store(self.path)
        two = self.command("create", payload, request_id)
        self.assertEqual(one, two)
        self.assertEqual(len(two["queue"]), 1)
        with self.assertRaises(OrderError):
            self.command("undo", {}, request_id)

    def test_concurrent_creates_and_duplicate_completions(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.create(), range(20)))
        state = self.store.read()
        self.assertEqual(len(state["queue"]), 20)
        self.assertEqual([o["number"] for o in state["queue"]], list(range(1, 21)))
        head_id = state["queue"][0]["id"]
        def complete(_):
            try:
                self.command("complete", {"id": head_id})
                return True
            except OrderError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(complete, range(4)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.store.read()["completed"]), 1)
        self.assertEqual(len(self.store.read()["queue"]), 19)

    def test_invalid_choices_are_rejected(self):
        for payload in [{}, {"flavors": ["mango"], "pearl": "apple", "size": "small"},
                        {"flavors": ["mango", "vanilla"], "pearl": "apple", "size": "large"},
                        {"flavors": ["mango", "mango"], "pearl": [], "size": "large"}]:
            with self.assertRaises(OrderError):
                self.command("create", payload)
        self.assertEqual(self.store.read()["revision"], 0)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.store = Store(Path(cls.temp.name) / "orders.sqlite3")
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def test_two_clients_see_same_order_and_static_files_are_restricted(self):
        payload = {"requestId": str(uuid.uuid4()), "action": "create",
                   "payload": {"flavors": ["mango", "strawberry"], "pearl": "watermelon", "size": "small"}}
        request = Request(self.url + "/api/commands", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json", "X-Boba-Client": "1"})
        with urlopen(request) as response:
            first = json.load(response)
        with urlopen(self.url + "/api/state") as response:
            second = json.load(response)
        self.assertEqual(first, second)
        with urlopen(self.url) as response:
            self.assertIn("Yoyoo Food Station", response.read().decode())
        for path in ["/server.py", "/data/orders.sqlite3", "/../server.py"]:
            with self.assertRaises(HTTPError) as error:
                urlopen(self.url + path)
            self.assertEqual(error.exception.code, 404)

    def test_plain_form_posts_are_rejected(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.url + "/api/commands", data=b"hello=world"))
        self.assertEqual(error.exception.code, 415)


if __name__ == "__main__":
    unittest.main(verbosity=2)
