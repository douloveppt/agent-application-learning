#!/usr/bin/env python3
"""A loopback-only, single-user HTTP/SQLite teaching service. Python 3.11+."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

OWNER_ID = "demo-user"
MAX_BODY = 16 * 1024
FIELD_LIMITS = {"title": 120, "description": 4000, "client_tag": 100}


class InputError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class TicketStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            with connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS tickets (
                        ticket_id TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        description TEXT NOT NULL,
                        client_tag TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)

    def connect(self) -> sqlite3.Connection:
        # Every caller creates and closes its own connection in its own thread.
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, fields: dict[str, str]) -> dict:
        ticket = {
            "ticket_id": "T-" + uuid.uuid4().hex,
            "owner_id": OWNER_ID,
            **fields,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?)",
                    tuple(ticket[key] for key in (
                        "ticket_id", "owner_id", "title", "description",
                        "client_tag", "status", "created_at",
                    )),
                )
        # The transaction has committed before this method returns.
        return ticket

    def get(self, ticket_id: str) -> dict | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE owner_id = ? AND ticket_id = ?",
                (OWNER_ID, ticket_id),
            ).fetchone()
        return dict(row) if row else None

    def list(self, client_tag: str | None = None) -> list[dict]:
        with closing(self.connect()) as connection:
            if client_tag is None:
                rows = connection.execute(
                    "SELECT * FROM tickets WHERE owner_id = ? ORDER BY created_at, ticket_id",
                    (OWNER_ID,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tickets WHERE owner_id = ? AND client_tag = ? "
                    "ORDER BY created_at, ticket_id", (OWNER_ID, client_tag),
                ).fetchall()
        return [dict(row) for row in rows]


def validate_ticket(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        raise InputError(422, "invalid_fields", "JSON root must be an object")
    if set(data) - FIELD_LIMITS.keys():
        raise InputError(422, "invalid_fields", "Only title, description and client_tag are allowed")
    fields = {}
    for name, limit in FIELD_LIMITS.items():
        value = data.get(name, "")
        if not isinstance(value, str) or len(value) > limit:
            raise InputError(422, "invalid_fields", f"{name} must be a string of at most {limit} characters")
        fields[name] = value.strip()
    if not fields["title"]:
        raise InputError(422, "invalid_fields", "title is required and must not be blank")
    return fields


class TicketServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, db_path: Path, port: int = 8765, log=None):
        self.store = TicketStore(db_path)
        self.process_requests = 0
        self.counter_lock = threading.Lock()
        self.log = log or (lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
        super().__init__(("127.0.0.1", port), TicketHandler)

    def next_request(self) -> tuple[str, int]:
        with self.counter_lock:
            self.process_requests += 1
            count = self.process_requests
        return "req-" + uuid.uuid4().hex, count


class TicketHandler(BaseHTTPRequestHandler):
    server: TicketServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        # Avoid BaseHTTPRequestHandler's unstructured request-line logging.
        pass

    def _begin(self):
        self.request_id, self.request_count = self.server.next_request()

    def _reply(self, status: int, payload: dict):
        body = json.dumps({"request_id": self.request_id, **payload}, ensure_ascii=False).encode("utf-8")
        delivered = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-ID", self.request_id)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            delivered = False
        finally:
            self.close_connection = True
        self.server.log({"request_id": self.request_id, "method": self.command,
                         "path": urlsplit(self.path).path, "status": status,
                         "response_written": delivered})

    def _dispatch(self, action):
        self._begin()
        try:
            action()
        except InputError as error:
            self._reply(error.status, {"error": {"code": error.code, "message": error.message}})
        except sqlite3.Error:
            self._reply(500, {"error": {"code": "storage_error", "message": "Database operation failed"}})

    def do_GET(self):
        self._dispatch(self._get)

    def _get(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query, keep_blank_values=True)
        if url.path == "/health" and not query:
            self._reply(200, {"status": "ok", "process_requests": self.request_count})
        elif url.path == "/tickets":
            if set(query) - {"client_tag"} or any(len(v) != 1 for v in query.values()):
                raise InputError(400, "invalid_query", "Only one client_tag filter is supported")
            client_tag = query.get("client_tag", [None])[0]
            if client_tag is not None and len(client_tag) > FIELD_LIMITS["client_tag"]:
                raise InputError(400, "invalid_query", "client_tag is too long")
            self._reply(200, {"tickets": self.server.store.list(client_tag)})
        elif url.path.startswith("/tickets/") and not query:
            ticket = self.server.store.get(url.path[len("/tickets/"):])
            if ticket is None:
                raise InputError(404, "not_found", "Ticket not found")
            self._reply(200, {"ticket": ticket})
        else:
            raise InputError(404, "not_found", "Endpoint not found")

    def do_POST(self):
        self._dispatch(self._post)

    def _post(self):
        url = urlsplit(self.path)
        if url.path != "/tickets":
            raise InputError(404, "not_found", "Endpoint not found")
        query = parse_qs(url.query, keep_blank_values=True)
        if set(query) - {"reply_delay_ms"} or any(len(v) != 1 for v in query.values()):
            raise InputError(400, "invalid_query", "Only one reply_delay_ms value is supported")
        try:
            delay_ms = int(query.get("reply_delay_ms", ["0"])[0])
        except ValueError:
            raise InputError(400, "invalid_query", "reply_delay_ms must be an integer") from None
        if not 0 <= delay_ms <= 2000:
            raise InputError(400, "invalid_query", "reply_delay_ms must be between 0 and 2000")
        if self.headers.get_content_type() != "application/json":
            raise InputError(415, "unsupported_media_type", "Use Content-Type: application/json")
        if self.headers.get("Transfer-Encoding"):
            raise InputError(400, "unsupported_transfer", "Use a Content-Length body in this lab")
        try:
            length = int(self.headers["Content-Length"])
        except (KeyError, TypeError, ValueError):
            raise InputError(411, "length_required", "A valid Content-Length is required") from None
        if length < 0:
            raise InputError(400, "invalid_length", "Content-Length must not be negative")
        if length > MAX_BODY:
            raise InputError(413, "body_too_large", "Request body exceeds 16 KiB")
        self.connection.settimeout(3)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")
            def invalid_constant(_):
                raise ValueError("non-JSON constant")
            data = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
        except (UnicodeDecodeError, ValueError, TimeoutError):
            raise InputError(400, "invalid_json", "Body must be complete UTF-8 JSON") from None
        fields = validate_ticket(data)
        ticket = self.server.store.create(fields)
        # Intentional fault injection: the write is committed before the delay.
        if delay_ms:
            time.sleep(delay_ms / 1000)
        self._reply(201, {"ticket": ticket})


def make_server(db_path: Path, port: int = 8765, log=None) -> TicketServer:
    return TicketServer(Path(db_path), port, log)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=Path("var/tickets.sqlite3"))
    args = parser.parse_args()
    with make_server(args.db, args.port) as server:
        print(f"Listening on http://127.0.0.1:{server.server_port}; database={args.db}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
