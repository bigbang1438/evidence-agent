"""Loopback-only HTTP application with bounded JSON and explicit approval routes."""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import threading
import time
from typing import Any

from .core import Agent, BudgetExceeded, EvidenceStore, ValidationError
from .ollama import ModelError, OllamaAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_REQUEST_BYTES = 768 * 1024
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"


class Application:
    def __init__(self, data_dir: Path, seed: bool = True, model: OllamaAdapter | None = None):
        self.store = EvidenceStore(Path(data_dir) / "state.db")
        self.csrf_token = secrets.token_urlsafe(32)
        self.model = model
        self.agent = Agent(self.store, model=model)
        self.run_lock = threading.Semaphore(1)
        self.rate_lock = threading.Lock()
        self.calls: dict[str, deque[float]] = defaultdict(deque)
        if seed and not self.store.documents():
            for source in sorted((PROJECT_ROOT / "samples").glob("*.md")):
                self.store.import_document(source.name, source.read_text(encoding="utf-8"))

    def state(self) -> dict[str, Any]:
        documents, tasks = self.store.documents(), self.store.tasks()
        return {"mode": "demo", "ollama_available": self.model is not None, "csrf_token": self.csrf_token,
                "documents": documents, "tasks": tasks, "pending_approvals": self.store.pending_approvals(),
                "counts": {"documents": len(documents), "chunks": sum(d["chunks"] for d in documents), "tasks": len(tasks)}}

    def allow_post(self, address: str) -> bool:
        current = time.monotonic()
        with self.rate_lock:
            queue = self.calls[address]
            while queue and queue[0] < current - 60:
                queue.popleft()
            if len(queue) >= 60:
                return False
            queue.append(current)
            return True


def validate_fields(body: Any, required: set[str], optional: set[str] | None = None) -> None:
    if not isinstance(body, dict) or not required <= set(body) or set(body) - required - (optional or set()):
        raise ValidationError("请求缺少必要字段或包含未支持的字段。")


def make_server(app: Application, port: int = 8788) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "EvidenceAgent"

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def send_data(self, status: int, payload: bytes, content_type: str, location: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(payload)

        def send_json(self, status: int, value: Any, location: str | None = None) -> None:
            self.send_data(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8", location)

        def fail(self, status: int, code: str, message: str) -> None:
            self.send_json(status, {"error": {"code": code, "message": message}})

        def guard(self, *, write: bool = False) -> bool:
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origins = {"http://" + host for host in hosts}
            if self.headers.get("Host", "") not in hosts or (self.headers.get("Origin") and self.headers["Origin"] not in origins) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.fail(403, "forbidden", "仅允许从本机同源页面访问。")
                return False
            if write:
                token = self.headers.get("X-CSRF-Token", "")
                if not token.isascii() or len(token) > 128 or not secrets.compare_digest(token, app.csrf_token):
                    self.fail(403, "forbidden", "请求校验失败，请刷新页面后重试。")
                    return False
                if not app.allow_post(self.client_address[0]):
                    self.fail(429, "rate_limited", "请求过于频繁，请稍后重试。")
                    return False
            return True

        def do_GET(self):
            if not self.guard():
                return
            if self.path == "/api/state":
                self.send_json(200, app.state())
            elif self.path == "/api/health":
                self.send_json(200, {"status": "ok", "ollama_configured": app.model is not None})
            elif self.path == "/api/tasks":
                self.send_json(200, {"items": app.store.tasks()})
            elif self.path in {"/", "/index.html", "/styles.css", "/app.js"}:
                name = "index.html" if self.path == "/" else self.path[1:]
                source = PROJECT_ROOT / "web" / name
                if not source.is_file():
                    self.fail(404, "not_found", "页面文件不存在。")
                    return
                types = {"index.html": "text/html; charset=utf-8", "styles.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
                self.send_data(200, source.read_bytes(), types[name])
            else:
                self.fail(404, "not_found", "页面或接口不存在。")

        def do_POST(self):
            if not self.guard(write=True):
                return
            try:
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    self.fail(415, "unsupported_media_type", "请发送 application/json 请求。")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_REQUEST_BYTES:
                    self.fail(413, "body_too_large", "请求体过大或缺少有效长度。")
                    return
                raw = self.rfile.read(length)
                if len(raw) != length:
                    self.fail(400, "invalid_json", "请求未完整传输。")
                    return
                try:
                    body = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeDecodeError):
                    self.fail(400, "invalid_json", "JSON 格式无效。")
                    return
                if self.path == "/api/documents":
                    validate_fields(body, {"filename", "content"})
                    document = app.store.import_document(body["filename"], body["content"])
                    self.send_json(201, {"document": document}, "/api/documents/" + document["id"])
                elif self.path == "/api/runs":
                    validate_fields(body, {"question"}, {"mode", "draft_task_title"})
                    if not app.run_lock.acquire(blocking=False):
                        self.fail(503, "busy", "已有请求正在执行，请完成后再试。")
                        return
                    try:
                        result = app.agent.run(body["question"], body.get("draft_task_title"), body.get("mode", "demo"))
                        self.send_json(200, result)
                    finally:
                        app.run_lock.release()
                elif self.path.startswith("/api/approvals/"):
                    validate_fields(body, {"approved"})
                    result = app.store.resolve_approval(self.path[len("/api/approvals/"):], body["approved"])
                    self.send_json(200, result)
                else:
                    self.fail(404, "not_found", "接口不存在。")
            except ValidationError as error:
                self.fail(422, "validation_error", str(error))
            except BudgetExceeded as error:
                self.fail(422, "budget_exceeded", str(error))
            except ModelError as error:
                self.fail(502, "model_unavailable", str(error))
            except (socket.timeout, TimeoutError):
                self.fail(408, "request_timeout", "请求超时，请重试。")
            except sqlite3.OperationalError:
                self.fail(503, "storage_busy", "存储暂时不可用，请稍后重试。")
            except Exception:
                logging.error("Request failed: %s", self.path)
                self.fail(500, "internal_error", "处理请求失败，请稍后重试。")

        def do_OPTIONS(self):
            self.fail(403, "forbidden", "不允许跨站请求。")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Evidence Agent 本地知识助手")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "runtime")
    parser.add_argument("--no-seed", action="store_true", help="不导入示例资料")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口应为 1–65535")
    model = None
    if os.environ.get("OLLAMA_MODEL"):
        try:
            model = OllamaAdapter(os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"), os.environ["OLLAMA_MODEL"])
        except ValidationError as error:
            parser.error(str(error))
    app = Application(args.data_dir, seed=not args.no_seed, model=model)
    server = make_server(app, args.port)
    print(f"Evidence Agent: http://127.0.0.1:{args.port} (demo available; ollama configured={model is not None})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
