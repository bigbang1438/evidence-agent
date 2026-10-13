"""Metrics are hand-derived from ranking positions, not model self-grades."""
import importlib


def test_retrieval_metrics_penalize_misses_and_rank_correctly():
    metrics = importlib.import_module("evidence_agent.evaluate").ranking_metrics([
        {"gold": ["a"], "ranked": ["a", "x"]},
        {"gold": ["b"], "ranked": ["x", "b"]},
        {"gold": ["c", "d"], "ranked": ["x", "c", "y"]},
        {"gold": ["z"], "ranked": []},
    ])
    assert metrics["recall_at_1"] == 0.25
    assert metrics["recall_at_3"] == 0.625
    assert metrics["mrr"] == 0.5


def test_traceability_detects_fabricated_text_and_invalid_line_numbers():
    validate = importlib.import_module("evidence_agent.evaluate").traceable
    source = "第一行\n第二行\n第三行"
    assert validate(source, {"text": "第二行\n第三行", "line_start": 2, "line_end": 3})
    assert not validate(source, {"text": "虚构内容", "line_start": 2, "line_end": 3})
    assert not validate(source, {"text": "第一行", "line_start": 0, "line_end": 1})
