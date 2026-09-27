"""Tests for the evaluation-dimension additions: error classification, cost
accounting, point-recall quality, and statistical significance.

Covers the changes that let a report say *which* dimension was measured and
*how confident* the cross-run verdict is.
"""

import math
import random

import pytest

from llm_evl.core.client import _compute_cost, _score_quality
from llm_evl.core.compare import compare_runs
from llm_evl.core.metrics import aggregate_requests
from llm_evl.core.models import (
    ErrorType,
    PromptItem,
    ProviderModel,
    RequestResult,
    Target,
)
from llm_evl.core.prompts import load_custom_prompts, save_custom_prompts
from llm_evl.core.stats import (
    betainc,
    min_detectable_rel_diff,
    required_n,
    t_cdf,
    t_ppf,
    two_proportion_ztest,
    welch_ttest,
)


# ---------------------------------------------------------------- error type

class TestErrorType:
    def test_status_mapping(self):
        assert ErrorType.from_status(429) is ErrorType.RATE_LIMIT
        assert ErrorType.from_status(400) is ErrorType.CLIENT_ERROR
        assert ErrorType.from_status(401) is ErrorType.CLIENT_ERROR
        assert ErrorType.from_status(404) is ErrorType.CLIENT_ERROR
        assert ErrorType.from_status(500) is ErrorType.SERVER_ERROR
        assert ErrorType.from_status(503) is ErrorType.SERVER_ERROR

    def test_success_has_empty_type(self):
        r = RequestResult(target="t", prompt_id="p", concurrency=1)
        assert r.error_type == ErrorType.NONE.value == ""


# -------------------------------------------------------------------- cost

class TestCost:
    def test_none_without_prices(self):
        t = Target(name="t", base_url="u", model="m")
        assert _compute_cost(t, 1000, 1000) is None

    def test_partial_prices(self):
        t = Target(name="t", base_url="u", model="m", price_out=2.0)
        # 1M output tokens at 2.0 -> 2.0; input unpriced -> 0
        assert _compute_cost(t, 1_000_000, 1_000_000) == pytest.approx(2.0)
        assert _compute_cost(t, 1_000_000, 0) == pytest.approx(0.0)

    def test_both_prices(self):
        t = Target(name="t", base_url="u", model="m", price_in=0.5, price_out=2.0)
        # 1M in @0.5 + 0.5M out @2.0 = 0.5 + 1.0
        assert _compute_cost(t, 1_000_000, 500_000) == pytest.approx(1.5)

    def test_zero_price_is_free_not_unknown(self):
        t = Target(name="t", base_url="u", model="m", price_in=0.0, price_out=0.0)
        assert _compute_cost(t, 1000, 1000) == pytest.approx(0.0)

    def test_prices_flow_from_provider_to_target(self):
        p = ProviderModel.from_dict(
            {"name": "m", "price_in": 1.5, "price_out": 6.0}
        )
        assert p.price_in == 1.5 and p.price_out == 6.0
        from llm_evl.core.models import Provider
        prov = Provider(name="v", base_url="http://x/v1", models=[p])
        t = prov.to_targets()[0]
        assert t.price_in == 1.5 and t.price_out == 6.0
        assert t.to_config_dict()["price_out"] == 6.0

    def test_prices_survive_roundtrip(self):
        p = ProviderModel.from_dict({"name": "m", "price_in": 0.25, "price_out": 1.0})
        assert ProviderModel.from_dict(p.to_dict()).price_in == 0.25


# ------------------------------------------------------------------ quality

class TestPointRecallQuality:
    def test_points_only(self):
        r = RequestResult(target="t", prompt_id="p", concurrency=1)
        _score_quality(r, "讲讲调用自己", 50, None, None, ["调用自己", "终止条件"])
        assert r.quality_point_hits == 1
        assert r.quality_point_total == 2
        assert r.quality_point_recall == pytest.approx(0.5)
        assert r.quality_score == pytest.approx(0.5)

    def test_all_points_hit_is_perfect(self):
        r = RequestResult(target="t", prompt_id="p", concurrency=1)
        _score_quality(r, "调用自己 终止条件 代码示例", 200, None, None,
                       ["调用自己", "终止条件", "代码示例"])
        assert r.quality_score == pytest.approx(1.0)
        assert r.quality_point_recall == pytest.approx(1.0)

    def test_points_dominate_but_do_not_erase_others(self):
        # All points hit but keyword and length both fail: score must be < 1.
        r = RequestResult(target="t", prompt_id="p", concurrency=1)
        _score_quality(r, "调用自己 终止条件 代码示例", 5, ["缺失词"], 500,
                       ["调用自己", "终止条件", "代码示例"])
        assert r.quality_point_recall == pytest.approx(1.0)
        assert r.quality_score < 1.0

    def test_points_beat_keywords_on_a_wrong_answer(self):
        """The failure mode that motivated reference_points: an on-topic but
        wrong answer still hits every keyword."""
        points = ["调用自己", "终止条件", "代码示例"]
        right = "递归是函数调用自己，需要终止条件。\n代码示例：def f(): ..."
        wrong = "递归很有趣，和函数调用有关。下面写代码示例和终止条件。"
        rr = RequestResult(target="t", prompt_id="p", concurrency=1)
        rw = RequestResult(target="t", prompt_id="p", concurrency=1)
        _score_quality(rr, right, 120, ["递归", "函数", "调用"], 100, points)
        _score_quality(rw, wrong, 120, ["递归", "函数", "调用"], 100, points)
        # Both hit all keywords equally; points must separate them.
        assert rr.quality_keyword_hits == rw.quality_keyword_hits == 3
        assert rr.quality_point_recall > rw.quality_point_recall
        assert rr.quality_score > rw.quality_score

    def test_legacy_weights_unchanged_without_points(self):
        """Existing scores must stay comparable to pre-change runs."""
        r = RequestResult(target="t", prompt_id="p", concurrency=1)
        _score_quality(r, "递归是一个函数调用自己", 120, ["递归", "函数"], 100)
        assert r.quality_score == pytest.approx(1.0)
        r2 = RequestResult(target="t", prompt_id="p", concurrency=1)
        _score_quality(r2, "递归很有趣", 30, ["递归", "函数"], 100)
        # 0.6*0.5 + 0.4*0.3
        assert r2.quality_score == pytest.approx(0.42)
        assert r2.quality_point_recall is None

    def test_blank_points_ignored(self):
        r = RequestResult(target="t", prompt_id="p", concurrency=1)
        _score_quality(r, "hello", 5, None, None, ["", "   "])
        assert r.quality_score is None

    def test_no_criteria_returns_none(self):
        r = RequestResult(target="t", prompt_id="p", concurrency=1)
        _score_quality(r, "hello", 2, None, None)
        assert r.quality_score is None


class TestPromptReferencePoints:
    def test_yaml_roundtrip(self, tmp_path):
        f = tmp_path / "prompts.yaml"
        save_custom_prompts([
            PromptItem(id="c1", label="Mine", text="t", bucket="medium",
                       reference_points=["要点A", "要点B"], custom=True),
        ], str(f))
        loaded = load_custom_prompts(str(f))
        assert len(loaded) == 1
        assert loaded[0].reference_points == ["要点A", "要点B"]

    def test_missing_key_defaults_to_empty(self, tmp_path):
        f = tmp_path / "prompts.yaml"
        f.write_text("prompts:\n  - id: c1\n    label: L\n    text: t\n",
                     encoding="utf-8")
        assert load_custom_prompts(str(f))[0].reference_points == []

    def test_builtin_prompts_declare_points(self):
        """At least one built-in must exercise the point-recall path, else the
        quality dimension is never covered for a default run."""
        from llm_evl.core.prompts import BUILTIN_PROMPTS
        assert any(p.reference_points for p in BUILTIN_PROMPTS)


class TestPromptApiSurface:
    """Regression: the API used to drop reference_points entirely, so the UI
    could neither display nor save them and quality stayed permanently
    uncovered even though the backend supported it."""

    def _manager(self, tmp_path, monkeypatch):
        """RunManager with prompts.yaml redirected to a temp file.

        PROMPTS_FILE is a *relative* path, so redirecting it by chdir would
        also move pytest's rootdir and break the rest of the session. Patch the
        loader/saver functions instead — they are imported inside the methods,
        so patching the source module is enough.
        """
        from llm_evl.api import run_manager as rm
        from llm_evl.core import prompts as prompts_mod

        store: list[PromptItem] = []

        def fake_load(path=None):
            return list(store)

        def fake_save(items, path=None):
            store.clear()
            store.extend(items)

        monkeypatch.setattr(prompts_mod, "load_custom_prompts", fake_load)
        monkeypatch.setattr(prompts_mod, "save_custom_prompts", fake_save)
        return rm.RunManager(), store

    def test_list_prompts_includes_reference_points(self, tmp_path, monkeypatch):
        m, _ = self._manager(tmp_path, monkeypatch)
        rows = m.list_prompts()
        assert rows, "expected built-in prompts"
        assert all("reference_points" in r for r in rows)
        assert any(r["reference_points"] for r in rows), \
            "no built-in prompt carries points, quality would never be covered"

    def test_save_then_list_preserves_reference_points(self, tmp_path, monkeypatch):
        m, store = self._manager(tmp_path, monkeypatch)
        m.save_custom_prompts([{
            "id": "c1", "label": "Mine", "text": "t", "bucket": "medium",
            "expected_keywords": ["k"], "min_output_tokens": 10,
            "reference_points": ["要点A", "要点B"],
        }])
        assert store and store[0].reference_points == ["要点A", "要点B"]
        got = [r for r in m.list_prompts() if r["id"] == "c1"]
        assert got, "saved prompt did not come back"
        assert got[0]["reference_points"] == ["要点A", "要点B"]


# ------------------------------------------------------------------ metrics

class TestAggregateNewFields:
    def test_error_breakdown_and_malformed(self):
        reqs = []
        for ok, et, bad in [(False, "timeout", 3), (False, "rate_limit", 0),
                            (False, "timeout", 1), (True, "", 2)]:
            r = RequestResult(target="t", prompt_id="p", concurrency=1)
            r.ok, r.error_type = ok, et
            r.malformed_chunks = bad
            if ok:
                r.ttft, r.e2e, r.output_tokens = 0.1, 1.0, 10
            reqs.append(r)
        a = aggregate_requests(reqs, 1)
        assert a.error_breakdown == {"timeout": 2, "rate_limit": 1}
        assert a.malformed_chunks == 6
        assert a.error_rate == pytest.approx(0.75)
        assert a.high_error is True

    def test_blank_error_type_falls_back_to_unknown(self):
        r = RequestResult(target="t", prompt_id="p", concurrency=1)
        r.ok, r.error_type = False, ""
        a = aggregate_requests([r], 1)
        assert a.error_breakdown == {"unknown": 1}

    def test_cost_and_token_totals(self):
        t = Target(name="t", base_url="u", model="m", price_in=1.0, price_out=4.0)
        reqs = []
        for _ in range(3):
            r = RequestResult(target="t", prompt_id="p", concurrency=1)
            r.ok = True
            r.ttft, r.e2e = 0.1, 1.0
            r.input_tokens, r.output_tokens = 1_000_000, 500_000
            r.cost = _compute_cost(t, r.input_tokens, r.output_tokens)
            reqs.append(r)
        a = aggregate_requests(reqs, 1)
        assert a.total_input_tokens == 3_000_000
        assert a.total_output_tokens == 1_500_000
        # 3 x (1M in @ 1.0 + 0.5M out @ 4.0) = 3 x 3.0
        assert a.total_cost == pytest.approx(9.0)

    def test_cost_none_when_unpriced(self):
        r = RequestResult(target="t", prompt_id="p", concurrency=1)
        r.ok, r.ttft, r.e2e, r.output_tokens = True, 0.1, 1.0, 10
        assert aggregate_requests([r], 1).total_cost is None

    def test_point_recall_aggregated(self):
        reqs = []
        for recall in (1.0, 0.5, 0.0, None):
            r = RequestResult(target="t", prompt_id="p", concurrency=1)
            r.ok, r.ttft, r.e2e, r.output_tokens = True, 0.1, 1.0, 10
            r.quality_point_recall = recall
            reqs.append(r)
        a = aggregate_requests(reqs, 1)
        assert a.quality_point_recall == pytest.approx(0.5)


# -------------------------------------------------------------------- stats

class TestStats:
    def test_betainc_symmetric_at_half(self):
        assert betainc(3, 3, 0.5) == pytest.approx(0.5)
        assert betainc(2, 5, 0.0) == 0.0
        assert betainc(2, 5, 1.0) == 1.0

    def test_t_cdf_known_values(self):
        assert t_cdf(0.0, 5) == pytest.approx(0.5)
        # Textbook t-table values.
        assert t_cdf(2.0, 10) == pytest.approx(0.963306, abs=1e-5)
        assert t_cdf(1.96, 1_000_000) == pytest.approx(0.975, abs=1e-4)

    def test_t_ppf_known_values(self):
        assert t_ppf(0.975, 10) == pytest.approx(2.228, abs=1e-3)
        assert t_ppf(0.95, 5) == pytest.approx(2.015, abs=1e-3)
        assert t_ppf(0.995, 30) == pytest.approx(2.750, abs=1e-3)

    def test_welch_basic(self):
        r = welch_ttest([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
        assert r is not None
        assert r["t"] == pytest.approx(-3 / math.sqrt(2.5))
        assert r["p_value"] == pytest.approx(0.107531, abs=1e-5)
        assert r["n_a"] == 5 and r["n_b"] == 5
        # CI must straddle zero since the difference is not significant.
        assert r["ci_low"] < 0 < r["ci_high"]

    def test_welch_detects_large_shift(self):
        # Constant samples have zero variance, so they are untestable by
        # design; real measurements always carry some jitter.
        rnd = random.Random(12)
        a = [rnd.gauss(1.0, 0.05) for _ in range(20)]
        b = [rnd.gauss(2.0, 0.05) for _ in range(20)]
        r = welch_ttest(a, b)
        assert r["p_value"] < 1e-8

    def test_welch_undefined_cases(self):
        assert welch_ttest([1.0], [2.0]) is None
        assert welch_ttest([1, 1, 1], [2, 2, 2]) is None

    def test_welch_p_value_in_unit_range(self):
        rnd = random.Random(4)
        for _ in range(50):
            a = [rnd.gauss(0, 1) for _ in range(8)]
            b = [rnd.gauss(rnd.uniform(-3, 3), rnd.uniform(0.1, 2)) for _ in range(11)]
            r = welch_ttest(a, b)
            assert 0.0 <= r["p_value"] <= 1.0

    def test_type_one_error_rate_is_calibrated(self):
        """Under H0 the rejection rate should sit near alpha, not far above it."""
        rnd = random.Random(2024)
        trials, rejected = 600, 0
        for _ in range(trials):
            a = [rnd.gauss(0, 1) for _ in range(20)]
            b = [rnd.gauss(0, 1) for _ in range(20)]
            if welch_ttest(a, b)["p_value"] < 0.05:
                rejected += 1
        rate = rejected / trials
        assert 0.02 < rate < 0.09, rate

    def test_two_proportion(self):
        r = two_proportion_ztest(0, 20, 4, 20)
        assert r is not None
        assert r["rate_a"] == 0.0 and r["rate_b"] == pytest.approx(0.2)
        assert r["p_value"] < 0.05

    def test_two_proportion_equal_rates_is_none(self):
        assert two_proportion_ztest(2, 20, 2, 20) is None

    def test_mde_shrinks_with_n(self):
        rnd = random.Random(5)
        mdes = [min_detectable_rel_diff([rnd.gauss(0.31, 0.12) for _ in range(n)])
                for n in (20, 80, 320)]
        assert mdes[0] > mdes[1] > mdes[2]
        # Halving the detectable effect needs ~4x the samples.
        assert mdes[0] / mdes[1] == pytest.approx(2.0, rel=0.25)

    def test_required_n_reaches_target_power(self):
        n = required_n(0.31, 0.12, 0.15)
        assert n > 20  # the current default is underpowered for a 15% change
        rnd = random.Random(6)
        hits, trials = 400, 0
        for _ in range(hits):
            a = [rnd.gauss(0.31, 0.12) for _ in range(n)]
            b = [rnd.gauss(0.31 * 1.15, 0.12) for _ in range(n)]
            if welch_ttest(a, b)["p_value"] < 0.05:
                trials += 1
        assert 0.68 < trials / hits < 0.92, trials / hits


# ------------------------------------------------------- compare integration

def _cell(target, samples, *, shift=1.0, err=0.0, seed=0):
    """Build a cell with real per-request samples so the test can run."""
    rnd = random.Random(seed)
    reqs, ttfts, tpss, e2es, itls = [], [], [], [], []
    for _ in range(samples):
        ok = rnd.random() > err
        ttft = rnd.gauss(0.30 * shift, 0.30 * 0.35) if ok else None
        tps = rnd.gauss(140, 20) if ok else None
        il = [rnd.gauss(0.02, 0.005) for _ in range(6)] if ok else []
        reqs.append({"ok": ok, "ttft": ttft, "e2e": ttft * 5 if ok else None,
                     "tokens_per_second": tps, "itl": il})
        if ok:
            ttfts.append(ttft); tpss.append(tps); e2es.append(ttft * 5); itls += il

    def q(v, p):
        if not v:
            return None
        s = sorted(v)
        return s[max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))]

    n_err = sum(1 for r in reqs if not r["ok"])
    return {
        "target": target, "prompt_id": "short", "prompt_label": "short",
        "bucket": "short", "concurrency": 1, "requests": reqs,
        "aggregates": {
            "n": samples, "n_errors": n_err, "error_rate": n_err / samples,
            "ttft_p50": q(ttfts, .5), "ttft_p90": q(ttfts, .9),
            "e2e_p50": q(e2es, .5), "itl_p50": q(itls, .5),
            "tokens_per_second_mean": sum(tpss) / len(tpss) if tpss else None,
            "high_error": False,
        },
    }


def _run(run_id, cells):
    return {"run_id": run_id, "started_at": 1.0, "status": "completed",
            "targets": [], "prompts": [], "cells": cells}


class TestCompareSignificance:
    def test_pure_noise_produces_no_regression(self):
        """The bug this replaces: jitter was reported as a regression."""
        base = _run("a", [_cell("m", 200, seed=1) for _ in range(4)])
        other = _run("b", [_cell("m", 200, seed=2) for _ in range(4)])
        r = compare_runs(base, other)
        assert r["n_worse"] == 0
        assert r["n_significant"] == 0
        assert r["n_tested"] > 0

    def test_real_regression_is_flagged(self):
        base = _run("a", [_cell("m", 200, seed=1) for _ in range(3)])
        other = _run("b", [_cell("m", 200, shift=1.4, seed=1) for _ in range(3)])
        r = compare_runs(base, other)
        assert r["n_worse"] > 0
        assert r["n_significant_worse"] > 0
        flagged = [
            d for row in r["rows"] for m, d in row["metrics"].items()
            if d["verdict"] == "worse" and d["pct"] is not None
        ]
        assert flagged
        # Order statistics of independent samples don't scale exactly, so the
        # observed shift lands near 40% rather than exactly on it. What
        # matters is that every flag is far above the 5% practical floor.
        assert all(abs(d["pct"]) >= 25 for d in flagged), \
            [d["pct"] for d in flagged]

    def test_error_rate_jump_from_zero_is_flagged(self):
        """error_rate == 0 is a real value, not missing data."""
        base = _run("a", [_cell("m", 200, seed=3) for _ in range(3)])
        other = _run("b", [_cell("m", 200, err=0.25, seed=3) for _ in range(3)])
        r = compare_runs(base, other)
        err_diffs = [row["metrics"]["error_rate"] for row in r["rows"]]
        assert any(d["verdict"] == "worse" for d in err_diffs)
        assert all(d["verdict"] != "na" for d in err_diffs)

    def test_impossible_zero_latency_stays_na(self):
        """TTFT of exactly 0 means 'no data', so the row must not be judged."""
        base = _run("a", [_cell("m", 20, seed=4)])
        other = _run("b", [_cell("m", 20, seed=4)])
        for c in base["cells"] + other["cells"]:
            c["aggregates"]["ttft_p50"] = 0.0
        r = compare_runs(base, other)
        assert r["rows"][0]["metrics"]["ttft_p50"]["verdict"] == "na"

    def test_resolution_is_reported(self):
        base = _run("a", [_cell("m", 200, seed=5)])
        other = _run("b", [_cell("m", 200, seed=6)])
        r = compare_runs(base, other)
        assert r["resolution_pct"] is not None
        assert 0 < r["resolution_pct"] < 100
        assert r["required_n"] and r["required_n"] > 0

    def test_lower_samples_means_worse_resolution(self):
        few = compare_runs(_run("a", [_cell("m", 20, seed=7)]),
                           _run("b", [_cell("m", 20, seed=8)]))
        many = compare_runs(_run("a", [_cell("m", 400, seed=7)]),
                            _run("b", [_cell("m", 400, seed=8)]))
        assert few["resolution_pct"] > many["resolution_pct"]

    def test_rows_without_samples_fall_back_to_legacy(self):
        """Old run files have no per-request data; they must still compare."""
        base = _run("a", [_cell("m", 20, seed=9)])
        other = _run("b", [_cell("m", 20, shift=2.0, seed=9)])
        for c in base["cells"] + other["cells"]:
            c["requests"] = []
        r = compare_runs(base, other)
        assert r["n_tested"] == 0
        assert r["n_worse"] > 0  # legacy percentage verdict still applies

    def test_p_values_and_ci_exposed(self):
        base = _run("a", [_cell("m", 200, seed=10)])
        other = _run("b", [_cell("m", 200, shift=1.5, seed=10)])
        r = compare_runs(base, other)
        d = r["rows"][0]["metrics"]["ttft_p50"]
        assert d["p_value"] is not None
        assert d["ci_low"] is not None and d["ci_high"] is not None
        # diff is (base - other), so the "other is slower" case is negative
        # and the whole interval sits below zero.
        assert d["ci_high"] < 0
        assert d["significant"] is True
