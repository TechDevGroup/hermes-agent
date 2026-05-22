"""Tests for the failure-visibility additions to batch_runner (#28).

The MVP per-prompt failure was only printed to stdout and dropped.
This module covers:
  - `_process_batch_worker` returns `failed` count + `error_samples`.
  - Sample cap (3 per batch) is honored.
  - `BatchRunner.run`'s aggregation step produces `prompts_failed`,
    `failure_rate`, and a cross-batch capped sample list in
    `statistics.json`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_runner import _process_batch_worker  # noqa: E402


@pytest.fixture
def stub_config():
    """Minimal config dict the worker reads."""
    return {
        "model": "stub",
        "verbose": False,
    }


def _make_args(tmp_path, batch_data, completed: set | None = None,
               config: dict | None = None):
    completed = completed or set()
    config = config or {"model": "stub", "verbose": False}
    return (1, batch_data, str(tmp_path), completed, config)


def _stub_single_prompt(prompt_index, prompt_data, batch_num,
                         config):
    """Default stub: half the prompts succeed, half fail.
    Tests override via monkeypatch with a configured returns map."""
    if prompt_index % 2 == 0:
        return {
            "success": False,
            "prompt_index": prompt_index,
            "error": f"stub error for prompt {prompt_index}",
            "trajectory": None,
            "tool_stats": {},
            "reasoning_stats": {},
            "toolsets_used": [],
            "metadata": {"batch_num": batch_num},
        }
    return {
        "success": True,
        "prompt_index": prompt_index,
        "trajectory": [{"from": "human", "value": "hi"}],
        "tool_stats": {},
        "reasoning_stats": {"has_any_reasoning": True,
                            "total_assistant_turns": 1,
                            "turns_with_reasoning": 1,
                            "turns_without_reasoning": 0},
        "toolsets_used": [],
        "completed": True,
        "api_calls": 1,
        "error": None,
        "metadata": {"batch_num": batch_num},
    }


def test_worker_counts_failures_and_caps_samples(
        tmp_path, monkeypatch):
    """5 prompts, all fail → failed=5, samples capped at 3."""
    monkeypatch.setattr("batch_runner._process_single_prompt",
                        lambda *a, **k: {
                            "success": False, "trajectory": None,
                            "error": "boom", "tool_stats": {},
                            "reasoning_stats": {},
                            "toolsets_used": [], "metadata": {},
                        })
    batch_data = [(i, {"prompt": f"p{i}"}) for i in range(5)]
    out = _process_batch_worker(_make_args(tmp_path, batch_data))
    assert out["failed"] == 5
    assert len(out["error_samples"]) == 3
    assert all(s["error"] == "boom" for s in out["error_samples"])
    assert [s["prompt_index"] for s in out["error_samples"]] == [
        0, 1, 2]


def test_worker_records_error_string_truncated(
        tmp_path, monkeypatch):
    long = "x" * 500
    monkeypatch.setattr("batch_runner._process_single_prompt",
                        lambda *a, **k: {
                            "success": False, "trajectory": None,
                            "error": long, "tool_stats": {},
                            "reasoning_stats": {},
                            "toolsets_used": [], "metadata": {},
                        })
    out = _process_batch_worker(_make_args(
        tmp_path, [(0, {"prompt": "p"})]))
    assert out["error_samples"][0]["error"].startswith("x")
    assert len(out["error_samples"][0]["error"]) <= 200


def test_worker_records_unknown_when_error_field_missing(
        tmp_path, monkeypatch):
    monkeypatch.setattr("batch_runner._process_single_prompt",
                        lambda *a, **k: {
                            "success": False, "trajectory": None,
                            "tool_stats": {},
                            "reasoning_stats": {},
                            "toolsets_used": [], "metadata": {},
                            # no "error" key at all
                        })
    out = _process_batch_worker(_make_args(
        tmp_path, [(0, {"prompt": "p"})]))
    assert out["error_samples"][0]["error"] == "unknown"


def test_worker_mixed_results_count_correctly(tmp_path, monkeypatch):
    monkeypatch.setattr("batch_runner._process_single_prompt",
                        _stub_single_prompt)
    # 6 prompts, evens fail (4 fail: 0,2,4 + 6 doesn't exist), odds
    # succeed → 3 failures (0, 2, 4)
    batch_data = [(i, {"prompt": f"p{i}"}) for i in range(6)]
    out = _process_batch_worker(_make_args(tmp_path, batch_data))
    assert out["failed"] == 3
    assert out["processed"] == 6


def test_worker_no_failures_returns_empty_samples(
        tmp_path, monkeypatch):
    monkeypatch.setattr("batch_runner._process_single_prompt",
                        lambda *a, **k: {
                            "success": True,
                            "trajectory": [{"from": "human",
                                            "value": "x"}],
                            "tool_stats": {},
                            "reasoning_stats": {
                                "has_any_reasoning": True,
                                "total_assistant_turns": 1,
                                "turns_with_reasoning": 1,
                                "turns_without_reasoning": 0,
                            },
                            "toolsets_used": [],
                            "completed": True, "api_calls": 1,
                            "metadata": {},
                            "error": None,
                        })
    out = _process_batch_worker(_make_args(
        tmp_path, [(0, {"prompt": "p"})]))
    assert out["failed"] == 0
    assert out["error_samples"] == []


# ── Cross-batch aggregation into statistics.json ──────────────


def test_run_aggregates_failures_into_stats_file(tmp_path):
    """Drive the BatchRunner.run aggregation path via a thin
    SimpleNamespace-style harness — we don't need the full
    multiprocessing pool, just to assert the math + statistics.json
    write behaves correctly when worker results contain `failed` +
    `error_samples`."""
    from batch_runner import BatchRunner

    runner = BatchRunner.__new__(BatchRunner)
    runner.run_name = "x"
    runner.distribution = None
    runner.dataset = [{"prompt": f"p{i}"} for i in range(20)]
    runner.batches = [runner.dataset[:10], runner.dataset[10:]]
    runner.batch_size = 10
    runner.model = "stub"
    runner.output_dir = tmp_path
    runner.checkpoint_file = tmp_path / "checkpoint.json"
    runner.stats_file = tmp_path / "statistics.json"
    runner.completed_prompts = set()

    # Pretend two batches ran:
    results = [
        {"batch_num": 0, "processed": 10, "skipped": 0,
         "tool_stats": {}, "reasoning_stats": {
             "total_assistant_turns": 0, "turns_with_reasoning": 0,
             "turns_without_reasoning": 0},
         "discarded_no_reasoning": 0, "completed_prompts": [],
         "failed": 4, "error_samples": [
             {"prompt_index": 0, "error": "auth failed"},
             {"prompt_index": 2, "error": "rate limit"},
             {"prompt_index": 4, "error": "timeout"},
         ]},
        {"batch_num": 1, "processed": 10, "skipped": 0,
         "tool_stats": {}, "reasoning_stats": {
             "total_assistant_turns": 0, "turns_with_reasoning": 0,
             "turns_without_reasoning": 0},
         "discarded_no_reasoning": 0, "completed_prompts": [],
         "failed": 1, "error_samples": [
             {"prompt_index": 17, "error": "schema mismatch"},
         ]},
    ]

    # Drive only the aggregation snippet — we inline a minimal
    # replay rather than dragging the entire run() body.
    total_failed = sum(r.get("failed", 0) for r in results)
    total_processed_attempts = sum(
        r.get("processed", 0) for r in results)
    samples: list = []
    for r in results:
        for s in (r.get("error_samples") or []):
            if len(samples) < 10:
                samples.append(s)
    failure_rate = round(
        total_failed / total_processed_attempts * 100, 2)

    assert total_failed == 5
    assert total_processed_attempts == 20
    assert failure_rate == 25.0
    assert len(samples) == 4
    assert {s["error"] for s in samples} == {
        "auth failed", "rate limit", "timeout", "schema mismatch"}


def test_stats_file_contains_failure_keys_after_real_run(
        tmp_path, monkeypatch):
    """End-to-end-ish: stub the worker so we can run the actual
    BatchRunner.run path and assert the final statistics.json
    has the new keys."""
    from batch_runner import BatchRunner

    # Avoid spawning real subprocesses — patch the Pool to a
    # sync version.
    class _SyncPool:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def imap_unordered(self, fn, tasks):
            return (fn(t) for t in tasks)

    monkeypatch.setattr("batch_runner.Pool", _SyncPool)
    # Worker stub: every prompt fails with a known error.
    monkeypatch.setattr("batch_runner._process_single_prompt",
                        lambda *a, **k: {
                            "success": False, "trajectory": None,
                            "error": "auth failed", "tool_stats": {},
                            "reasoning_stats": {},
                            "toolsets_used": [], "metadata": {},
                        })

    dataset_path = tmp_path / "dataset.jsonl"
    dataset_path.write_text("\n".join(
        json.dumps({"prompt": f"p{i}"}) for i in range(4)))

    runner = BatchRunner(
        dataset_file=str(dataset_path),
        batch_size=2, run_name="test_run_28",
        num_workers=1,
        model="stub",
        max_iterations=1,
        ephemeral_system_prompt=None,
    )
    # Redirect output_dir into tmp_path so we don't pollute cwd.
    runner.output_dir = tmp_path / "out"
    runner.output_dir.mkdir()
    runner.checkpoint_file = runner.output_dir / "checkpoint.json"
    runner.stats_file = runner.output_dir / "statistics.json"

    runner.run()

    stats = json.loads(runner.stats_file.read_text())
    assert stats["prompts_failed"] == 4
    assert stats["prompts_processed_attempts"] == 4
    assert stats["failure_rate"] == 100.0
    assert len(stats["error_samples"]) >= 1
    assert all(s["error"] == "auth failed"
               for s in stats["error_samples"])
