"""Offline retrieval and source traceability evaluation, never QA accuracy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .core import EvidenceStore


def ranking_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    answerable = [row for row in rows if row["gold"]]
    if not answerable:
        return {"recall_at_1": 0.0, "recall_at_3": 0.0, "mrr": 0.0}
    scores: dict[str, float] = {}
    for k in (1, 3):
        scores[f"recall_at_{k}"] = sum(len(set(row["gold"]) & set(row["ranked"][:k])) / len(set(row["gold"])) for row in answerable) / len(answerable)
    scores["mrr"] = sum(next((1 / i for i, item in enumerate(row["ranked"], 1) if item in row["gold"]), 0) for row in answerable) / len(answerable)
    return scores


def traceable(source: str, hit: dict[str, Any]) -> bool:
    lines = source.splitlines()
    start, end = hit["line_start"], hit["line_end"]
    return 1 <= start <= end <= len(lines) and hit["text"] == "\n".join(lines[start - 1:end])


def evaluate(dataset_path: Path) -> dict[str, Any]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    rows, verified, retrieved = [], 0, 0
    with TemporaryDirectory(prefix="evidence-eval-") as temp:
        store = EvidenceStore(Path(temp) / "eval.db")
        for filename, content in dataset["documents"].items():
            store.import_document(filename, content)
        for case in dataset["questions"]:
            hits = store.search(case["question"], limit=3)
            ranking = [f"{h['filename']}:{h['line_start']}-{h['line_end']}" for h in hits]
            verified += sum(traceable(dataset["documents"][h["filename"]], h) for h in hits)
            retrieved += len(hits)
            rows.append({"id": case["id"], "question": case["question"], "gold": case["gold"], "ranked": ranking})
    negatives = [row for row in rows if not row["gold"]]
    return {"dataset": dataset["name"], "questions": len(rows), "answerable": len(rows) - len(negatives), "negative_questions": len(negatives), "metrics": {**ranking_metrics(rows), "source_traceability": verified / retrieved if retrieved else 0, "negative_abstention_rate": sum(not row["ranked"] for row in negatives) / len(negatives) if negatives else 0}, "retrieved_chunks": retrieved, "verified_source_chunks": verified, "scope": "原创小型固定词法检索集；source_traceability 仅检查片段及行号可溯源，不代表结论正确、引用语义支持、LLM问答准确率或泛化表现。", "cases": rows}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Evidence Agent 离线检索评测")
    parser.add_argument("--dataset", type=Path, default=root / "evaluation" / "dataset.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.dataset)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
