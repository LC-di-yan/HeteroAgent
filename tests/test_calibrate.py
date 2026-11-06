"""检索阈值校准测试（T-P2-01）。"""
from __future__ import annotations

import pytest

from evaluation.calibrate import calibrate, collect_scores


class _Case:
    def __init__(self, query, expected_source):
        self.query = query
        self.expected_source = expected_source


class _Memory:
    def __init__(self, mapping):
        self.mapping = mapping

    def search(self, query, top_k=5):
        return [{"id": "d", "content": "x", "source": "s", "score": s} for s in self.mapping.get(query, [])]


def test_separable_classes_pick_middle():
    result = calibrate([0.8, 0.9], [0.1, 0.2])
    assert result.accuracy == 1.0
    assert 0.2 <= result.suggested_threshold <= 0.8
    assert result.margin == pytest.approx(0.6)


def test_overlapping_classes_maximise_accuracy():
    result = calibrate([0.3, 0.6], [0.2, 0.5])
    assert 0.0 <= result.suggested_threshold <= 1.0
    assert result.accuracy == max(p["accuracy"] for p in result.curve)
    assert result.margin is not None and result.margin < 0


def test_current_threshold_reported():
    result = calibrate([0.8], [0.2], current_threshold=0.5)
    assert result.current_threshold == 0.5
    assert result.current_accuracy == 1.0


def test_empty_inputs_rejected():
    with pytest.raises(ValueError):
        calibrate([], [])


def test_all_miss_cases_still_calibrates():
    result = calibrate([], [0.1, 0.2])
    assert result.suggested_threshold >= 0.2
    assert result.accuracy == 1.0


def test_collect_scores_splits_by_expected_source():
    memory = _Memory({"q1": [0.9, 0.3], "q2": [0.2], "q3": []})
    cases = [_Case("q1", "product_faq.md"), _Case("q2", "out_of_scope"), _Case("q3", "out_of_scope")]
    hits, misses = collect_scores(memory, cases, top_k=5)
    assert hits == [0.9]
    assert misses == [0.2, 0.0]


def test_serialisation_shape():
    data = calibrate([0.7], [0.1], current_threshold=0.3).to_dict()
    assert {"suggested_threshold", "accuracy", "hit", "miss", "margin", "current", "curve"} <= set(data)
    assert data["hit"]["count"] == 1
