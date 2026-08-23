"""Metric aggregation: quantiles (p50/p90/p99) and throughput rollups."""

from __future__ import annotations

import statistics

from .models import CellAggregates, RequestResult


def quantile(values: list[float], p: float) -> float | None:
    """Linear-interpolation quantile. Returns None for empty input.

    p is a fraction in [0, 1].
    """
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    s = sorted(values)
    if p <= 0:
        return s[0]
    if p >= 1:
        return s[-1]
    # Linear interpolation between closest ranks (numpy-compatible).
    rank = p * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def aggregate_requests(requests: list[RequestResult], concurrency: int) -> CellAggregates:
    """Compute CellAggregates from a cell's request results."""
    ok = [r for r in requests if r.ok]
    errs = [r for r in requests if not r.ok]
    n = len(requests)
    error_rate = (len(errs) / n) if n else 0.0

    ttfts = [r.ttft for r in ok if r.ttft is not None]
    ttss = [r.tts for r in ok if r.tts is not None]
    e2es = [r.e2e for r in ok if r.e2e is not None]
    itls: list[float] = []
    for r in ok:
        itls.extend(r.itl)
    tps = [r.tokens_per_second for r in ok if r.tokens_per_second is not None]
    total_tokens = sum(r.output_tokens for r in ok)
    quality_scores = [r.quality_score for r in ok if r.quality_score is not None]

    # Keyword hit ratio across requests that measured keywords.
    kw_hits = [r.quality_keyword_hits for r in ok if r.quality_score is not None]
    kw_ratio: float | None = None
    if kw_hits:
        # Average per-request hit ratio is hard without the denominator; use
        # mean of (hits/total) per request approximated by mean hits. We expose
        # the mean quality_score instead; keyword_hit_ratio = mean hits / max
        # expected (approximate). Simpler: report mean quality_score only.
        kw_ratio = statistics.fmean(kw_hits) if kw_hits else None

    # Aggregate throughput across all concurrent requests in the cell:
    # total output tokens / wall-clock generation window. Only meaningful at
    # concurrency > 1 (Q4); we still compute it at c=1 (equals mean tps there).
    agg_tps: float | None = None
    if ok:
        gen_spans = [r.generation_duration for r in ok if r.generation_duration and r.generation_duration > 0]
        if gen_spans:
            # Sum of per-request generation work / max wall-clock span approximates
            # aggregate throughput under closed-loop concurrency.
            wall = max(r.e2e for r in ok if r.e2e is not None) if any(r.e2e for r in ok) else None
            if wall and wall > 0:
                agg_tps = total_tokens / wall

    return CellAggregates(
        n=n,
        n_errors=len(errs),
        error_rate=error_rate,
        ttft_p50=quantile(ttfts, 0.5),
        ttft_p90=quantile(ttfts, 0.9),
        ttft_p99=quantile(ttfts, 0.99),
        e2e_p50=quantile(e2es, 0.5),
        e2e_p90=quantile(e2es, 0.9),
        itl_p50=quantile(itls, 0.5),
        itl_p90=quantile(itls, 0.9),
        tts_p50=quantile(ttss, 0.5),
        tts_p90=quantile(ttss, 0.9),
        tokens_per_second_mean=statistics.fmean(tps) if tps else None,
        tokens_per_second_p50=quantile(tps, 0.5),
        aggregate_tokens_per_second=agg_tps,
        total_output_tokens=total_tokens,
        quality_mean=statistics.fmean(quality_scores) if quality_scores else None,
        quality_keyword_hit_ratio=kw_ratio,
        high_error=error_rate > 0.5,
    )
