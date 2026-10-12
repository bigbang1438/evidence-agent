"""Bounded lexical retrieval, durable evidence, and approval-controlled tools."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterator
from uuid import uuid4

MAX_DOCUMENT_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_DOCUMENTS = 30
CHUNK_SIZE = 600
RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class ValidationError(ValueError):
    """Input is invalid or conflicts with durable state."""


class ApprovalRequired(PermissionError):
    """A write tool requires a separate, explicit approval."""


class BudgetExceeded(RuntimeError):
    """The fixed execution plan exceeds its time or step budget."""


def bounded_text(value: Any, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValidationError(f"{name}需为 1–{max_length} 字的非空文本。")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValidationError(f"{name}包含不支持的控制字符。")
    return value.strip()


def validate_filename(value: Any) -> str:
    name = bounded_text(value, "文件名", 80)
    if not re.fullmatch(r"[\w ()\-]+\.(?:md|txt)", name, flags=re.IGNORECASE):
        raise ValidationError("只支持不含路径的 .md 或 .txt 文件名。")
    if name.split(".")[0].casefold() in RESERVED_NAMES:
        raise ValidationError("文件名使用了系统保留名称。")
    return name


def tokenize(text: str) -> list[str]:
    """Latin words plus CJK characters and bigrams; no semantic embeddings."""
    tokens = re.findall(r"[a-z0-9_]+", text.casefold())
    for group in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(group)
        tokens.extend(group[i:i + 2] for i in range(len(group) - 1))
    return tokens


def split_chunks(content: str) -> list[dict[str, Any]]:
    """Pack lines into bounded chunks while retaining original line numbers."""
    chunks: list[dict[str, Any]] = []
    buffer: list[str] = []
    start = end = size = 0
    for line_number, line in enumerate(content.splitlines(), 1):
        pieces = [line[i:i + CHUNK_SIZE] for i in range(0, len(line), CHUNK_SIZE)] or [""]
        for piece in pieces:
            if buffer and size + 1 + len(piece) > CHUNK_SIZE:
                if "\n".join(buffer).strip():
                    chunks.append({"text": "\n".join(buffer), "line_start": start, "line_end": end})
                buffer, size = [], 0
            if not buffer:
                start = line_number
            buffer.append(piece)
            size += len(piece) + (1 if len(buffer) > 1 else 0)
            end = line_number
    if buffer and "\n".join(buffer).strip():
        chunks.append({"text": "\n".join(buffer), "line_start": start, "line_end": end})
    return chunks


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EvidenceStore:
    """SQLite transactions isolate imports and approval consumption."""

    def __init__(self, database: Path | str):
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    content TEXT NOT NULL, byte_size INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    text TEXT NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                    approval_id TEXT UNIQUE
                );
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_document(self, filename: str, content: str) -> dict[str, Any]:
        name = validate_filename(filename)
        bounded_text(content, "文档", MAX_DOCUMENT_BYTES)
        text = content
        byte_size = len(text.encode("utf-8"))
        if byte_size > MAX_DOCUMENT_BYTES:
            raise ValidationError("单个文档不得超过 256 KiB。")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        chunks = split_chunks(text)
        doc_id = uuid4().hex
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                count, used = db.execute("SELECT COUNT(*), COALESCE(SUM(byte_size), 0) FROM documents").fetchone()
                if count >= MAX_DOCUMENTS or used + byte_size > MAX_TOTAL_BYTES:
                    raise ValidationError("知识库最多 30 个文档、合计 1 MiB。")
                db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?)", (doc_id, name, text, byte_size, now()))
                db.executemany(
                    "INSERT INTO chunks (document_id, text, line_start, line_end) VALUES (?, ?, ?, ?)",
                    [(doc_id, item["text"], item["line_start"], item["line_end"]) for item in chunks],
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError("已有同名文档；请使用新的文件名。") from exc
        return {"id": doc_id, "filename": name, "characters": len(text), "chunks": len(chunks)}

    def documents(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT d.id, d.filename, LENGTH(d.content) AS characters, COUNT(c.id) AS chunks
                FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                GROUP BY d.id ORDER BY d.created_at, d.filename
            """)]

    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        query = bounded_text(query, "检索问题", 2000)
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValidationError("检索数量需为 1–5。")
        query_terms = set(tokenize(query))
        if not query_terms:
            return []
        with self.connection() as db:
            rows = [dict(row) for row in db.execute("""
                SELECT c.document_id, d.filename, c.text, c.line_start, c.line_end
                FROM chunks c JOIN documents d ON d.id=c.document_id ORDER BY c.id
            """)]
        if not rows:
            return []
        frequencies = [Counter(tokenize(row["text"])) for row in rows]
        lengths = [sum(freq.values()) for freq in frequencies]
        average_length = sum(lengths) / len(lengths) or 1
        doc_frequencies = Counter(term for freq in frequencies for term in freq if term in query_terms)
        ranked = []
        for row, freq, length in zip(rows, frequencies, lengths):
            score = 0.0
            for term in query_terms:
                tf = freq[term]
                if not tf:
                    continue
                df = doc_frequencies[term]
                idf = math.log(1 + (len(rows) - df + 0.5) / (df + 0.5))
                score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * length / average_length))
            if score > 0:
                ranked.append({**row, "score": round(score, 6)})
        return sorted(ranked, key=lambda row: row["score"], reverse=True)[:limit]

    def tasks(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT id, title, created_at FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT 100")]

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT id, title, created_at FROM approvals WHERE status='pending' ORDER BY rowid DESC LIMIT 100")]

    def propose_task(self, title: str) -> dict[str, Any]:
        title = bounded_text(title, "任务标题", 120)
        approval = {"id": uuid4().hex, "title": title, "created_at": now()}
        with self.connection() as db:
            if db.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0] >= 100:
                raise ValidationError("待确认任务已达 100 条，请先处理现有任务。")
            db.execute("INSERT INTO approvals (id, title, created_at) VALUES (?, ?, ?)", tuple(approval.values()))
        return approval

    @staticmethod
    def _insert_task(db: sqlite3.Connection, title: str, approval_id: str | None = None) -> dict[str, Any]:
        task = {"id": uuid4().hex, "title": title, "created_at": now()}
        db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?)", (*task.values(), approval_id))
        return task

    def create_approved_task(self, title: str) -> dict[str, Any]:
        title = bounded_text(title, "任务标题", 120)
        with self.connection() as db:
            return self._insert_task(db, title)

    def resolve_approval(self, approval_id: str, approved: bool) -> dict[str, Any]:
        if not isinstance(approval_id, str) or not re.fullmatch(r"[a-f0-9]{32}", approval_id):
            raise ValidationError("确认编号无效。")
        if type(approved) is not bool:
            raise ValidationError("确认选择需为布尔值。")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            approval = db.execute("SELECT * FROM approvals WHERE id=? AND status='pending'", (approval_id,)).fetchone()
            if approval is None:
                raise ValidationError("确认请求不存在或已处理。")
            status = "approved" if approved else "rejected"
            db.execute("UPDATE approvals SET status=? WHERE id=?", (status, approval_id))
            result: dict[str, Any] = {"status": status}
            if approved:
                result["task"] = self._insert_task(db, approval["title"], approval_id)
            return result


class ToolRegistry:
    """No shell, arbitrary network, or dynamic tool registration is exposed."""
    names = frozenset({"knowledge_search", "task_create", "task_list"})

    def __init__(self, store: EvidenceStore):
        self.store = store

    def invoke(self, name: str, arguments: dict[str, Any], *, approved: bool = False) -> Any:
        if not isinstance(name, str) or name not in self.names or not isinstance(arguments, dict):
            raise ValidationError("工具不在白名单中或参数无效。")
        expected = {"knowledge_search": {"query"}, "task_create": {"title"}, "task_list": set()}[name]
        if set(arguments) != expected:
            raise ValidationError("工具参数与声明不匹配。")
        if name == "knowledge_search":
            return self.store.search(arguments["query"])
        if name == "task_list":
            return self.store.tasks()
        if approved is not True:
            raise ApprovalRequired("创建任务需要人工确认。")
        return self.store.create_approved_task(arguments["title"])


class Agent:
    """A deterministic plan; a model may only synthesize retrieved evidence."""

    def __init__(self, store: EvidenceStore, max_steps: int = 6, timeout_seconds: float = 20, model: Any = None):
        self.store, self.tools = store, ToolRegistry(store)
        self.max_steps, self.timeout_seconds, self.model = max_steps, timeout_seconds, model

    def run(self, question: str, draft_task_title: str | None = None, mode: str = "demo") -> dict[str, Any]:
        question = bounded_text(question, "问题", 2000)
        if not isinstance(mode, str) or mode not in {"demo", "ollama"}:
            raise ValidationError("不支持的运行模式。")
        if mode == "ollama" and self.model is None:
            raise ValidationError("未配置本地模型，请使用演示模式。")
        if draft_task_title is not None:
            draft_task_title = bounded_text(draft_task_title, "待创建任务", 120)
        started = time.monotonic()
        steps: list[dict[str, Any]] = []

        def guard() -> None:
            if len(steps) >= self.max_steps or time.monotonic() - started >= self.timeout_seconds:
                raise BudgetExceeded("执行超过步骤或时间预算，未继续调用工具。")

        def execute(name: str, arguments: dict[str, Any]) -> Any:
            guard()
            step_start = time.monotonic()
            value = self.tools.invoke(name, arguments)
            summary = f"检索到 {len(value)} 个片段" if name == "knowledge_search" else f"读取 {len(value)} 条任务"
            steps.append({"index": len(steps) + 1, "tool": name, "status": "success", "duration_ms": round((time.monotonic() - step_start) * 1000, 2), "summary": summary})
            return value

        citations = execute("knowledge_search", {"query": question})
        if "任务" in question or "待办" in question:
            tasks = execute("task_list", {})
        else:
            tasks = None
        if not citations:
            answer = "未找到匹配资料。请导入相关文档，或使用文档中的关键词重试。"
        elif mode == "demo":
            answer = "以下为关键词检索到的原文片段，演示模式未使用大模型生成答案：\n\n" + "\n\n".join(f"[{i}] {item['text']}" for i, item in enumerate(citations, 1))
        else:
            guard()
            model_started = time.monotonic()
            answer = self.model.answer(question, citations, timeout=max(0.1, self.timeout_seconds - (time.monotonic() - started)))
            steps.append({"index": len(steps) + 1, "tool": "ollama_answer", "status": "success", "duration_ms": round((time.monotonic() - model_started) * 1000, 2), "summary": "根据检索片段生成答案；请核对来源"})
        if tasks is not None:
            answer += f"\n\n当前已确认任务共显示 {len(tasks)} 条。"
        pending = None
        if draft_task_title is not None:
            guard()
            task_start = time.monotonic()
            try:
                self.tools.invoke("task_create", {"title": draft_task_title})
            except ApprovalRequired:
                pending = self.store.propose_task(draft_task_title)
            steps.append({"index": len(steps) + 1, "tool": "task_create", "status": "awaiting_confirmation", "duration_ms": round((time.monotonic() - task_start) * 1000, 2), "summary": "已提出任务，尚未写入任务列表"})
        return {"id": uuid4().hex, "mode": mode, "answer": answer, "citations": citations, "steps": steps, "pending_approval": pending, "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
