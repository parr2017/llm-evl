"""Tests for cross-run comparison (compare_runs + /api/compare endpoint)."""

import pytest

from llm_evl.core.compare import METRICS, compare_runs


def make_run(run_id, cells):
    return {
        "run_id": run_id,
        "started_at": 1787000000.0,
        "status": "completed",
        "targets": [{"name": c["target"] for c in cells}],
        "prompts": [],
        "cells": cells,
    }


def make_cell(target, prompt_id, conc, ttft, tps, err=0.0):
    return {
        "target": target,
        "prompt_id": prompt_id,
        "prompt_label": f"label-{prompt_id}",
        "bucket": "short",
        "concurrency": conc,
        "requests": [],
        "aggregates": {
            "n": 10,
            "n_errors": int(err * 10),
            "error_rate": err,
            "ttft_p50": ttft,
            "ttft_p90": ttft * 1.5,
            "e2e_p50": ttft + 1.0,
            "itl_p50": 0.05,
            "tokens_per_second_mean": tps,
            "total_output_tokens": 1000,
            "high_error": err > 0.5,
        },
    }


class TestCompareRuns:
    def test_basic_alignment_and_verdicts(self):
        base = make_run("aaa", [make_cell("t1", "short", 1, ttft=2.0, tps=50)])
        other = make_run("bbb", [make_cell("t1", "short", 1, ttft=1.0, tps=75)])

        result = compare_runs(base, other)

        assert result["n_matched"] == 1
        assert result["n_base_only"] == 0
        assert result["n_other_only"] == 0
        row = result["rows"][0]
        assert (row["target"], row["prompt_id"], row["concurrency"]) == ("t1", "short", 1)

        m = row["metrics"]
        # TTFT halved -> better (lower is better)
        assert m["ttft_p50"]["verdict"] == "better"
        assert m["ttft_p50"]["pct"] == pytest.approx(-50.0)
        assert m["ttft_p50"]["delta"] == pytest.approx(-1.0)
        # Throughput up 50% -> better (higher is better)
        assert m["tokens_per_second_mean"]["verdict"] == "better"
        assert m["tokens_per_second_mean"]["pct"] == pytest.approx(50.0)

    def test_worse_regression(self):
        base = make_run("aaa", [make_cell("t1", "short", 1, ttft=1.0, tps=50)])
        other = make_run("bbb", [make_cell("t1", "short", 1, ttft=3.0, tps=50)])

        result = compare_runs(base, other)
        m = result["rows"][0]["metrics"]
        assert m["ttft_p50"]["verdict"] == "worse"
        assert m["ttft_p50"]["pct"] == pytest.approx(200.0)
        assert m["tokens_per_second_mean"]["verdict"] == "same"  # identical value

    def test_unmatched_cells_counted(self):
        base = make_run("aaa", [
            make_cell("t1", "short", 1, ttft=1.0, tps=50),
            make_cell("t1", "long", 4, ttft=2.0, tps=40),   # only in base
        ])
        other = make_run("bbb", [
            make_cell("t1", "short", 1, ttft=1.5, tps=45),
            make_cell("t2", "short", 1, ttft=9.9, tps=9),   # only in other
        ])

        result = compare_runs(base, other)
        assert result["n_matched"] == 1
        assert result["n_base_only"] == 1
        assert result["n_other_only"] == 1
        assert result["rows"][0]["target"] == "t1"
        assert result["rows"][0]["prompt_id"] == "short"

    def test_summary_is_mean_of_pcts(self):
        base = make_run("aaa", [
            make_cell("t1", "short", 1, ttft=1.0, tps=50),
            make_cell("t1", "short", 2, ttft=2.0, tps=60),
        ])
        other = make_run("bbb", [
            make_cell("t1", "short", 1, ttft=2.0, tps=50),
            make_cell("t1", "short", 2, ttft=4.0, tps=90),
        ])

        result = compare_runs(base, other)
        # ttft p50 pct: +100%, +100% -> mean 100%
        assert result["summary"]["ttft_p50"] == pytest.approx(100.0)
        # tokens/s pct: 0%, +50% -> mean 25%
        assert result["summary"]["tokens_per_second_mean"] == pytest.approx(25.0)

    def test_null_and_zero_base_handled(self):
        cell_b = make_cell("t1", "short", 1, ttft=0.0, tps=None)
        cell_o = make_cell("t1", "short", 1, ttft=1.0, tps=40)
        result = compare_runs(make_run("a", [cell_b]), make_run("b", [cell_o]))
        m = result["rows"][0]["metrics"]
        assert m["ttft_p50"]["verdict"] == "na"
        assert m["tokens_per_second_mean"]["verdict"] == "na"

    def test_all_metrics_present_in_each_row(self):
        base = make_run("aaa", [make_cell("t1", "short", 1, ttft=1.0, tps=50)])
        other = make_run("bbb", [make_cell("t1", "short", 1, ttft=1.1, tps=55)])
        result = compare_runs(base, other)
        assert set(result["rows"][0]["metrics"].keys()) == {name for name, _, _ in METRICS}

    def test_meta_returned(self):
        base = make_run("aaa", [make_cell("t1", "short", 1, ttft=1.0, tps=50)])
        other = make_run("bbb", [make_cell("t1", "short", 1, ttft=1.1, tps=55)])
        result = compare_runs(base, other)
        assert result["base"]["run_id"] == "aaa"
        assert result["other"]["run_id"] == "bbb"


class TestCompareApi:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient
        from llm_evl.api import run_manager
        from llm_evl.api.server import create_app

        runs = {
            "aaa": make_run("aaa", [make_cell("t1", "short", 1, ttft=1.0, tps=50)]),
            "bbb": make_run("bbb", [make_cell("t1", "short", 1, ttft=0.8, tps=60)]),
        }
        monkeypatch.setattr(run_manager.manager, "get_run", lambda rid: runs.get(rid))
        app = create_app(config_path="targets.yaml")
        return TestClient(app)

    def test_endpoint_ok(self, client):
        resp = client.get("/api/compare", params={"base": "aaa", "other": "bbb"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["base"]["run_id"] == "aaa"
        assert data["other"]["run_id"] == "bbb"
        assert data["n_matched"] == 1
        assert data["rows"][0]["metrics"]["ttft_p50"]["verdict"] == "better"

    def test_endpoint_missing_run_404(self, client):
        resp = client.get("/api/compare", params={"base": "nope", "other": "bbb"})
        assert resp.status_code == 404


class TestGetRunApi:
    """Regression: GET /api/runs/{id} must return the run body, not null."""

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient
        from llm_evl.api import run_manager
        from llm_evl.api.server import create_app

        runs = {"aaa": make_run("aaa", [make_cell("t1", "short", 1, ttft=1.0, tps=50)])}
        monkeypatch.setattr(run_manager.manager, "get_run", lambda rid: runs.get(rid))
        app = create_app(config_path="targets.yaml")
        return TestClient(app)

    def test_get_run_returns_body(self, client):
        resp = client.get("/api/runs/aaa")
        assert resp.status_code == 200
        data = resp.json()
        assert data is not None
        assert data["run_id"] == "aaa"
        assert len(data["cells"]) == 1

    def test_get_run_missing_404(self, client):
        resp = client.get("/api/runs/nope")
        assert resp.status_code == 404
