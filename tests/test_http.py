"""Real local HTTP requests exercise routes, CSRF, and request boundaries."""
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

import pytest

from evidence_agent.server import Application, make_server


@contextmanager
def running_app(tmp_path):
    app = Application(tmp_path, seed=False)
    server = make_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", app
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def request(url, path, data=None, headers=None, raw=None):
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else raw
    req = Request(url + path, data=body, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        response = urlopen(req, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        payload = response.read()
        try:
            payload = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            pass
        return response.status, payload, response.headers


def state_and_token(url):
    status, state, _ = request(url, "/api/state")
    assert status == 200
    return state, {"X-CSRF-Token": state["csrf_token"]}


def test_state_exposes_empty_collections_and_disabled_model(tmp_path):
    with running_app(tmp_path) as (url, _):
        state, _ = state_and_token(url)
        assert state["documents"] == state["tasks"] == state["pending_approvals"] == []
        assert state["ollama_available"] is False
        assert state["mode"] == "demo"


def test_post_without_csrf_token_cannot_import_document(tmp_path):
    with running_app(tmp_path) as (url, app):
        status, payload, _ = request(url, "/api/documents", {"filename": "notes.md", "content": "hello"})
        assert status == 403
        assert payload["error"]["code"] == "forbidden"
        assert app.store.documents() == []


def test_import_run_and_approval_complete_normal_flow(tmp_path):
    with running_app(tmp_path) as (url, _):
        _, headers = state_and_token(url)
        status, result, response_headers = request(url, "/api/documents", {"filename": "tools.md", "content": "工具写入需要人工确认。"}, headers)
        assert status == 201
        assert result["document"]["chunks"] == 1
        assert response_headers["Location"].startswith("/api/documents/")
        status, run, _ = request(url, "/api/runs", {"question": "工具写入", "mode": "demo", "draft_task_title": "检查确认链路"}, headers)
        assert status == 200
        assert run["citations"][0]["filename"] == "tools.md"
        status, before, _ = request(url, "/api/tasks")
        assert before["items"] == []
        status, decision, _ = request(url, "/api/approvals/" + run["pending_approval"]["id"], {"approved": True, "conversation_id": run["conversation_id"]}, headers)
        assert status == 200
        assert decision["task"]["title"] == "检查确认链路"
        assert request(url, "/api/tasks")[1]["items"][0]["title"] == "检查确认链路"


def test_cross_origin_request_is_rejected_even_with_token(tmp_path):
    with running_app(tmp_path) as (url, app):
        _, headers = state_and_token(url)
        status, _, _ = request(url, "/api/documents", {"filename": "notes.md", "content": "hello"}, {**headers, "Origin": "https://evil.test"})
        assert status == 403
        assert app.store.documents() == []


def test_host_header_rejects_dns_rebinding(tmp_path):
    with running_app(tmp_path) as (url, _):
        status, _, _ = request(url, "/api/state", headers={"Host": "attacker.test"})
        assert status == 403


@pytest.mark.parametrize("payload", [{"filename": "../notes.md", "content": "hello"}, {"filename": "notes.md", "content": "hello", "path": "D:/secret"}])
def test_document_route_rejects_bad_paths_and_unknown_fields(tmp_path, payload):
    with running_app(tmp_path) as (url, app):
        _, headers = state_and_token(url)
        status, _, _ = request(url, "/api/documents", payload, headers)
        assert status == 422
        assert app.store.documents() == []


def test_malformed_json_returns_400_without_traceback(tmp_path):
    with running_app(tmp_path) as (url, _):
        _, headers = state_and_token(url)
        status, result, _ = request(url, "/api/runs", headers=headers, raw=b"{broken")
        assert status == 400
        assert result["error"]["code"] == "invalid_json"
        assert "Traceback" not in str(result)


def test_write_tool_is_not_an_http_endpoint(tmp_path):
    with running_app(tmp_path) as (url, app):
        _, headers = state_and_token(url)
        status, _, _ = request(url, "/api/tools/task_create", {"title": "绕过", "approved": True}, headers)
        assert status == 404
        assert app.store.tasks() == []


def test_local_files_outside_static_allowlist_are_not_served(tmp_path):
    with running_app(tmp_path) as (url, _):
        assert request(url, "/../README.md")[0] == 404
        assert request(url, "/%2e%2e/README.md")[0] == 404


def test_responses_include_csp_and_no_cross_origin_permission(tmp_path):
    with running_app(tmp_path) as (url, _):
        _, headers = state_and_token(url)
        status, _, response_headers = request(url, "/api/state")
        assert status == 200
        assert "script-src 'self'" in response_headers["Content-Security-Policy"]
        assert "unsafe-inline" not in response_headers["Content-Security-Policy"]
        assert response_headers.get("Access-Control-Allow-Origin") is None


def test_declared_request_size_is_rejected_before_body_upload(tmp_path):
    with running_app(tmp_path) as (url, app):
        _, headers = state_and_token(url)
        address = urlsplit(url)
        connection = HTTPConnection(address.hostname, address.port, timeout=5)
        connection.putrequest("POST", "/api/documents")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(768 * 1024 + 1))
        connection.putheader("X-CSRF-Token", headers["X-CSRF-Token"])
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 413
        assert json.loads(response.read())["error"]["code"] == "body_too_large"
        connection.close()
        assert app.store.documents() == []


def test_invalid_mode_returns_422_instead_of_crashing(tmp_path):
    with running_app(tmp_path) as (url, _):
        _, headers = state_and_token(url)
        status, result, _ = request(url, "/api/runs", {"question": "工具", "mode": {}}, headers)
        assert status == 422
        assert result["error"]["code"] == "validation_error"


def test_single_active_run_blocks_concurrent_execution(tmp_path):
    with running_app(tmp_path) as (url, app):
        _, headers = state_and_token(url)
        app.run_lock.acquire()
        try:
            status, result, _ = request(url, "/api/runs", {"question": "工具"}, headers)
            assert status == 503
            assert result["error"]["code"] == "busy"
        finally:
            app.run_lock.release()


def test_write_rate_limit_returns_429_before_sixty_first_operation(tmp_path):
    with running_app(tmp_path) as (url, app):
        _, headers = state_and_token(url)
        for _ in range(60):
            assert app.allow_post("127.0.0.1")
        status, result, _ = request(url, "/api/runs", {"question": "工具"}, headers)
        assert status == 429
        assert result["error"]["code"] == "rate_limited"


def test_static_page_and_javascript_are_served(tmp_path):
    with running_app(tmp_path) as (url, _):
        status, body, headers = request(url, "/")
        assert status == 200
        assert "Evidence Agent" in body.decode("utf-8")
        assert headers["Content-Type"].startswith("text/html")
        assert request(url, "/app.js")[0] == 200
        assert request(url, "/styles.css")[0] == 200


def test_non_ascii_csrf_header_is_rejected_safely(tmp_path):
    with running_app(tmp_path) as (url, app):
        status, result, _ = request(url, "/api/documents", {"filename": "note.md", "content": "hello"}, {"X-CSRF-Token": "é"})
        assert status == 403
        assert result["error"]["code"] == "forbidden"
        assert app.store.documents() == []


def test_conversation_api_creates_thread_and_persists_two_runs(tmp_path):
    with running_app(tmp_path) as (url, _):
        _, headers = state_and_token(url)
        status, created, _ = request(url, "/api/conversations", {"title": "检索实验"}, headers)
        assert status == 201
        thread_id = created["conversation"]["id"]
        for question in ["首次提问", "第二次提问"]:
            status, run, _ = request(url, "/api/runs", {"question": question, "conversation_id": thread_id}, headers)
            assert status == 200
            assert run["conversation_id"] == thread_id
        status, thread, _ = request(url, "/api/conversations/" + thread_id)
        assert status == 200
        assert [r["question"] for r in thread["runs"]] == ["首次提问", "第二次提问"]
        assert request(url, "/api/state")[1]["conversations"][0]["turns"] == 2


def test_conversation_create_requires_csrf_and_valid_title(tmp_path):
    with running_app(tmp_path) as (url, app):
        assert request(url, "/api/conversations", {"title": "未授权"})[0] == 403
        _, headers = state_and_token(url)
        assert request(url, "/api/conversations", {"title": "x" * 81}, headers)[0] == 422
        assert app.store.conversations() == []


def test_invalid_conversation_route_cannot_read_arbitrary_files(tmp_path):
    with running_app(tmp_path) as (url, _):
        assert request(url, "/api/conversations/../../README.md")[0] == 422


