"""Local HTTP contract tests, not measurements of a real model."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from evidence_agent.core import ValidationError
from evidence_agent.ollama import ModelError, OllamaAdapter


@contextmanager
def ollama_stub(payload=None, status=200):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            observed.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps(payload or {"message": {"role": "assistant", "content": "工具写入需要确认 [1]"}, "done": True}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_chat_posts_non_streaming_messages_and_reads_assistant_content():
    with ollama_stub() as (url, observed):
        adapter = OllamaAdapter(url, "test-model")
        answer = adapter.answer("怎样创建任务？", [{"filename": "tools.md", "line_start": 3, "line_end": 3, "text": "写入需人工确认"}], timeout=2)
        assert answer == "工具写入需要确认 [1]"
        path, payload = observed[0]
        assert path == "/api/chat"
        assert payload["model"] == "test-model"
        assert payload["stream"] is False
        assert payload["messages"][0]["role"] == "system"
        assert "tools.md:3-3" in payload["messages"][1]["content"]
        assert "写入需人工确认" in payload["messages"][1]["content"]
        assert "tools" not in payload


@pytest.mark.parametrize("url", ["https://example.com", "http://10.0.0.1:11434", "http://localhost@evil.test", "http://localhost:11434/other", "http://localhost:11434?x=1"])
def test_model_address_rejects_remote_or_ambiguous_urls(url):
    with pytest.raises(ValidationError):
        OllamaAdapter(url, "test-model")


def test_model_error_is_safe_and_does_not_expose_response_body():
    with ollama_stub({"error": "sensitive backend details"}, status=500) as (url, _):
        with pytest.raises(ModelError) as error:
            OllamaAdapter(url, "test-model").answer("question", [], timeout=2)
        assert "sensitive" not in str(error.value)


def test_missing_message_is_rejected_instead_of_used_as_answer():
    with ollama_stub({"message": {"role": "assistant"}, "done": True}) as (url, _):
        with pytest.raises(ModelError):
            OllamaAdapter(url, "test-model").answer("question", [], timeout=2)


def test_chat_accepts_tool_calls_and_serializes_tools_with_tool_feedback():
    reply = {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "knowledge_search", "arguments": {"query": "审批"}}}]}, "done": True}
    with ollama_stub(reply) as (url, observed):
        from evidence_agent.loop import TOOLS
        message = OllamaAdapter(url, "test-model").chat([{"role": "user", "content": "审批"}, {"role": "tool", "tool_name": "knowledge_search", "content": "[]"}], TOOLS, timeout=2)
        assert message["tool_calls"][0]["function"]["arguments"] == {"query": "审批"}
        assert observed[0][1]["tools"][0]["function"]["name"] == "knowledge_search"
        assert observed[0][1]["messages"][1]["tool_name"] == "knowledge_search"
        assert observed[0][1]["stream"] is False


@pytest.mark.parametrize("message", [{"content": "", "tool_calls": "not a list"}, {"content": "", "tool_calls": [{"function": []}]}, {"content": "", "tool_calls": [{"function": {"name": "x", "arguments": "{}"}}]}])
def test_chat_rejects_malformed_tool_payloads(message):
    with ollama_stub({"message": message, "done": True}) as (url, _):
        with pytest.raises(ModelError):
            OllamaAdapter(url, "test-model").chat([{"role": "user", "content": "x"}], [], timeout=2)

