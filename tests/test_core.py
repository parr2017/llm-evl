"""Pure unit tests for core logic (no network)."""

from llm_evl.core.metrics import quantile, aggregate_requests
from llm_evl.core.models import (
    CellAggregates,
    PromptItem,
    RequestResult,
    Target,
    TokenSource,
)
from llm_evl.core.prompts import get_prompts, BUILTIN_PROMPTS
from llm_evl.core.tokenizer import count_tokens


def test_quantile_empty_and_single():
    assert quantile([], 0.5) is None
    assert quantile([5.0], 0.5) == 5.0
    assert quantile([5], 0.0) == 5
    assert quantile([5], 1.0) == 5


def test_quantile_linear_interpolation():
    vals = [float(i) for i in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]]
    assert quantile(vals, 0.5) == 5.5
    q90 = quantile(vals, 0.9)
    assert q90 is not None and round(q90, 3) == 9.1
    assert quantile(vals, 0.0) == 1
    assert quantile(vals, 1.0) == 10


def test_tokenizer_usage_preferred_over_tiktoken():
    n, src = count_tokens("hello world", {"completion_tokens": 3})
    assert src == TokenSource.USAGE.value
    assert n == 3


def test_tokenizer_alias_output_tokens():
    n, src = count_tokens("hi", {"output_tokens": 7})
    assert src == TokenSource.USAGE.value and n == 7


def test_tokenizer_falls_back_to_tiktoken():
    n, src = count_tokens("hello world this is a test", None)
    assert src == TokenSource.TIKTOKEN.value
    assert n > 0


def test_tokenizer_empty_text_no_usage():
    n, src = count_tokens("", None)
    assert n == 0 and src == TokenSource.NONE.value


def test_aggregate_requests_builds_quantiles():
    reqs = []
    for i in range(5):
        r = RequestResult(target="t", prompt_id="short", concurrency=4)
        r.ok = True
        r.ttft = 0.1 + i * 0.02
        r.e2e = 1.0 + i * 0.1
        r.generation_duration = r.e2e - r.ttft
        r.output_tokens = 50 + i
        r.tokens_per_second = r.output_tokens / r.generation_duration
        r.itl = [0.02, 0.03, 0.025]
        reqs.append(r)
    agg = aggregate_requests(reqs, 4)
    assert isinstance(agg, CellAggregates)
    assert agg.n == 5 and agg.n_errors == 0 and agg.error_rate == 0.0
    assert agg.ttft_p50 is not None
    assert agg.tokens_per_second_mean is not None
    assert agg.total_output_tokens == 50 + 51 + 52 + 53 + 54


def test_aggregate_requests_high_error_flag():
    reqs = []
    for i in range(4):
        r = RequestResult(target="t", prompt_id="short", concurrency=1)
        r.ok = i < 1  # 3 errors out of 4 -> 75%
        reqs.append(r)
    agg = aggregate_requests(reqs, 1)
    assert agg.n == 4 and agg.n_errors == 3
    assert agg.error_rate == 0.75
    assert agg.high_error is True


def test_builtin_prompts_three_buckets():
    ps = get_prompts()
    assert len(ps) == 3
    assert {p.bucket for p in ps} == {"short", "medium", "long"}


def test_builtin_prompts_carry_quality_config():
    from llm_evl.core.prompts import BUILTIN_PROMPTS
    medium = next(p for p in BUILTIN_PROMPTS if p.id == "medium")
    assert medium.expected_keywords  # non-empty
    assert medium.min_output_tokens is not None and medium.min_output_tokens > 0
    short = next(p for p in BUILTIN_PROMPTS if p.id == "short")
    assert not short.expected_keywords  # quality not measured for greeting


def test_get_prompts_filter_by_id():
    ps = get_prompts(prompt_ids=["short"])
    assert len(ps) == 1 and ps[0].id == "short"


def test_target_resolved_api_key_plaintext_wins(monkeypatch):
    monkeypatch.setenv("MY_KEY", "envval")
    t = Target(name="x", base_url="http://x/v1", model="m",
               api_key="plain", api_key_env="MY_KEY")
    assert t.resolved_api_key() == "plain"
    assert t.has_plaintext_key() is True


def test_target_resolved_api_key_env_fallback(monkeypatch):
    monkeypatch.setenv("MY_KEY", "envval")
    t = Target(name="x", base_url="http://x/v1", model="m", api_key_env="MY_KEY")
    assert t.resolved_api_key() == "envval"
    assert t.has_plaintext_key() is False


def test_target_to_config_dict_hides_key():
    t = Target(name="x", base_url="http://x/v1", model="m", api_key="secret")
    d = t.to_config_dict()
    assert "api_key" not in d
    assert d["has_api_key"] is True


def test_quality_score_keyword_and_length():
    from llm_evl.core.client import _score_quality
    r = RequestResult(target="t", prompt_id="m", concurrency=1)
    # both keywords hit, length ok -> high score
    _score_quality(r, "递归是一个函数调用自己", n_tokens=120,
                   expected_keywords=["递归", "函数"], min_output_tokens=100)
    assert r.quality_score is not None
    assert r.quality_keyword_hits == 2
    assert r.quality_length_ok is True
    assert r.quality_score == 1.0

    # one keyword missing, length short -> lower score
    r2 = RequestResult(target="t", prompt_id="m", concurrency=1)
    _score_quality(r2, "递归很有趣", n_tokens=30,
                   expected_keywords=["递归", "函数"], min_output_tokens=100)
    assert r2.quality_keyword_hits == 1
    assert r2.quality_length_ok is False
    assert 0.3 < r2.quality_score < 0.7


def test_quality_score_no_criteria_returns_none():
    from llm_evl.core.client import _score_quality
    r = RequestResult(target="t", prompt_id="s", concurrency=1)
    _score_quality(r, "hello", n_tokens=2, expected_keywords=None, min_output_tokens=None)
    assert r.quality_score is None


def test_aggregate_includes_tts_and_quality():
    reqs = []
    for i in range(4):
        r = RequestResult(target="t", prompt_id="m", concurrency=2)
        r.ok = True
        r.ttft = 0.1
        r.tts = 0.2 + i * 0.01
        r.e2e = 1.0
        r.generation_duration = 0.9
        r.output_tokens = 100
        r.tokens_per_second = 100 / 0.9
        r.quality_score = 0.5 + i * 0.1
        reqs.append(r)
    agg = aggregate_requests(reqs, 2)
    assert agg.tts_p50 is not None and agg.tts_p90 is not None
    qm = agg.quality_mean
    assert qm is not None
    assert 0.5 < float(qm) < 0.9, qm
