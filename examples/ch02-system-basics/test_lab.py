from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest

from async_demo import run_mode
from server import make_server


@contextmanager
def serving(db_path):
    events = queue.Queue()
    server = make_server(db_path, port=0, log=events.put)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server, events
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, method, path, data=None, *, raw=None, headers=None, timeout=2):
    headers = dict(headers or {})
    if data is not None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
    try:
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    finally:
        connection.close()


class HttpLabTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "tickets.sqlite3"

    def test_create_query_chinese_and_request_ids(self):
        with serving(self.db) as (server, _):
            status, headers, created = request(server, "POST", "/tickets", {
                "title": "VPN 无法连接", "description": "macOS，连接超时", "client_tag": "normal-01",
            }, headers={"X-User": "untrusted-user"})
            self.assertEqual(status, 201)
            self.assertEqual(headers["X-Request-ID"], created["request_id"])
            ticket = created["ticket"]
            self.assertEqual(ticket["owner_id"], "demo-user")
            status, headers, fetched = request(server, "GET", "/tickets/" + ticket["ticket_id"])
            self.assertEqual(status, 200)
            self.assertEqual(fetched["ticket"], ticket)
            self.assertNotEqual(fetched["request_id"], created["request_id"])
            self.assertEqual(headers["X-Request-ID"], fetched["request_id"])

    def test_invalid_json_and_fields_do_not_write(self):
        cases = [
            (b"{", 400), (b"NaN", 400), (b"[]", 422),
            (b'{"title":"   "}', 422), (b'{"title":3}', 422),
            (b'{"title":"VPN", "owner_id":"other"}', 422),
            (b'{"title":"VPN", "description":null}', 422),
        ]
        with serving(self.db) as (server, _):
            for raw, expected in cases:
                with self.subTest(raw=raw):
                    status, _, body = request(server, "POST", "/tickets", raw=raw,
                                              headers={"Content-Type": "application/json"})
                    self.assertEqual(status, expected)
                    self.assertIn("error", body)
            self.assertEqual(server.store.list(), [])

    def test_media_type_length_limit_and_unknown_route(self):
        with serving(self.db) as (server, _):
            status, _, _ = request(server, "POST", "/tickets", raw=b"{}",
                                   headers={"Content-Type": "text/plain"})
            self.assertEqual(status, 415)
            status, _, _ = request(server, "POST", "/tickets", raw=b" " * 16385,
                                   headers={"Content-Type": "application/json"})
            self.assertEqual(status, 413)
            status, _, body = request(server, "GET", "/tickets/not-an-existing-ticket")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")
            self.assertEqual(server.store.list(), [])

    def test_invalid_delay_never_creates_a_ticket(self):
        with serving(self.db) as (server, _):
            for query in ("reply_delay_ms=-1", "reply_delay_ms=2001", "reply_delay_ms=abc", "unexpected=1"):
                status, _, _ = request(server, "POST", "/tickets?" + query, {"title": "VPN"})
                self.assertEqual(status, 400)
            self.assertEqual(server.store.list(), [])

    def test_restart_persists_data_and_resets_memory(self):
        with serving(self.db) as (server, _):
            _, _, created = request(server, "POST", "/tickets", {"title": "持久记录"})
            _, _, health = request(server, "GET", "/health")
            self.assertEqual(health["process_requests"], 2)
            ticket_id = created["ticket"]["ticket_id"]
        with serving(self.db) as (server, _):
            _, _, health = request(server, "GET", "/health")
            self.assertEqual(health["process_requests"], 1)
            status, _, fetched = request(server, "GET", "/tickets/" + ticket_id)
            self.assertEqual(status, 200)
            self.assertEqual(fetched["ticket"]["title"], "持久记录")
        with serving(self.db.with_name("different.sqlite3")) as (server, _):
            _, _, listed = request(server, "GET", "/tickets")
            self.assertEqual(listed["tickets"], [])

    def test_concurrent_requests_keep_data_separate(self):
        with serving(self.db) as (server, _):
            def create(index):
                return request(server, "POST", "/tickets", {"title": f"问题-{index}", "client_tag": str(index)})
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(create, (1, 2)))
            self.assertEqual([status for status, _, _ in results], [201, 201])
            tickets = [body["ticket"] for _, _, body in results]
            self.assertEqual(len({ticket["ticket_id"] for ticket in tickets}), 2)
            self.assertEqual({(ticket["title"], ticket["client_tag"]) for ticket in tickets},
                             {("问题-1", "1"), ("问题-2", "2")})

    def test_timeout_after_commit_still_leaves_record(self):
        with serving(self.db) as (server, events):
            with self.assertRaises(TimeoutError):
                request(server, "POST", "/tickets?reply_delay_ms=150", {
                    "title": "响应丢失演示", "client_tag": "timeout-01",
                }, timeout=0.03)
            # Synchronize on observable database state, rather than guessing a sleep.
            deadline = time.monotonic() + 1
            rows = []
            while not rows and time.monotonic() < deadline:
                rows = server.store.list("timeout-01")
                if not rows:
                    time.sleep(0.005)
            self.assertEqual(len(rows), 1)
            event = events.get(timeout=2)
            self.assertEqual(event["status"], 201)
            status, _, listed = request(server, "GET", "/tickets?client_tag=timeout-01")
            self.assertEqual(status, 200)
            self.assertEqual(listed["tickets"][0]["ticket_id"], rows[0]["ticket_id"])

    def test_repeated_post_is_not_idempotent(self):
        with serving(self.db) as (server, _):
            for _ in range(2):
                request(server, "POST", "/tickets", {"title": "重复提交", "client_tag": "same-tag"})
            _, _, listed = request(server, "GET", "/tickets?client_tag=same-tag")
            self.assertEqual(len(listed["tickets"]), 2)

    def test_sql_special_characters_remain_data(self):
        with serving(self.db) as (server, _):
            title = "VPN'; DROP TABLE tickets; --"
            _, _, created = request(server, "POST", "/tickets", {"title": title})
            _, _, listed = request(server, "GET", "/tickets")
            self.assertEqual(listed["tickets"][0]["title"], title)
            self.assertEqual(listed["tickets"][0]["ticket_id"], created["ticket"]["ticket_id"])


class AsyncLabTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_modes_preserve_both_results(self):
        for mode in ("sequential", "concurrent", "blocking"):
            with self.subTest(mode=mode):
                output = await run_mode(mode)
                self.assertEqual(output["status"], "ok")
                self.assertEqual([value["source"] for value in output["results"]], ["knowledge", "profile"])

    async def test_blocking_events_are_serial(self):
        output = await run_mode("blocking")
        events = [(event["task"], event["event"]) for event in output["events"]]
        self.assertLess(events.index(("knowledge", "completed")), events.index(("profile", "started")))

    async def test_timeout_cancels_local_simulation(self):
        output = await run_mode("timeout")
        self.assertEqual(output["status"], "timeout")
        self.assertIn(("knowledge", "cancelled"), [(e["task"], e["event"]) for e in output["events"]])
        self.assertEqual(output["results"], [])

    async def test_taskgroup_failure_cancels_sibling(self):
        output = await run_mode("failure")
        self.assertEqual(output["status"], "failed")
        events = [(event["task"], event["event"]) for event in output["events"]]
        self.assertIn(("knowledge", "failed"), events)
        self.assertIn(("profile", "cancelled"), events)
        self.assertNotIn(("profile", "completed"), events)


if __name__ == "__main__":
    unittest.main()
