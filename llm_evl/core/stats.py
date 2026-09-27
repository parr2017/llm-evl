"""Welch's t-test for benchmark comparison.

compare.py used to label any cell whose metric moved more than
SAME_THRESHOLD_PCT as better/worse. With n=20 samples a 5% swing is pure
noise, so that produced phantom regressions. Here we test the actual
difference of means and only call it a regression when it is significant.

Deliberately dependency-free: the p-value needs the Student-t CDF, which is
built on the regularized incomplete beta function. That is ~40 lines of
classical numerics (Lentz's continued fraction), cheaper than adding scipy.

All functions are pure and return None when the sample cannot support a test
(e.g. n < 2, or zero variance in both groups), so callers can fall back to
the legacy percentage heuristic.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# Guard against pathological continued-fraction loops.
_MAX_ITER = 300
_EPS = 3.0e-16
_TINY = 1.0e-300


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method).

    Returns the part of I_x(a,b) that is not the closed-form prefactor.
    """
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _TINY:
        d = _TINY
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_ITER + 1):
        m2 = 2 * m
        # Even step.
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        h *= d * c
        # Odd step.
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_cdf(t: float, df: float) -> float:
    """CDF of the Student-t distribution with `df` degrees of freedom."""
    if df <= 0:
        raise ValueError("df must be positive")
    if t == 0.0:
        return 0.5
    # P(T > t) = 0.5 * I_{df/(df+t^2)}(df/2, 1/2) for t > 0.
    x = df / (df + t * t)
    tail = 0.5 * betainc(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def t_ppf(p: float, df: float) -> float:
    """Inverse CDF, by bisection. Accurate to ~1e-10, which is plenty here."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    lo, hi = -1.0e3, 1.0e3
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _mean_var(xs: Sequence[float]) -> tuple[float, float]:
    n = len(xs)
    m = sum(xs) / n
    if n < 2:
        return m, 0.0
    # Sample variance (n-1 denominator).
    return m, sum((x - m) ** 2 for x in xs) / (n - 1)


def welch_ttest(a: Sequence[float], b: Sequence[float],
                confidence: float = 0.95) -> dict | None:
    """Two-sided Welch t-test on two independent samples.

    Returns None when the test is undefined (either sample has n < 2, or both
    have zero variance so the standard error is 0). On success returns:

        t, df, p_value, mean_a, mean_b, diff, se, ci_low, ci_high, n_a, n_b
    """
    xs = [float(x) for x in a if x is not None]
    ys = [float(x) for x in b if x is not None]
    if len(xs) < 2 or len(ys) < 2:
        return None

    ma, va = _mean_var(xs)
    mb, vb = _mean_var(ys)
    na, nb = len(xs), len(ys)

    sa2, sb2 = va / na, vb / nb
    se2 = sa2 + sb2
    if se2 <= 0.0:
        return None
    se = math.sqrt(se2)

    t = (ma - mb) / se
    # Welch-Satterthwaite dof.
    denom = (sa2 ** 2) / (na - 1) + (sb2 ** 2) / (nb - 1)
    df = (se2 ** 2) / denom if denom > 0 else float(na + nb - 2)

    p_value = 2.0 * (1.0 - t_cdf(abs(t), df))
    # Clamp: t_cdf can return 1.0 for a huge t, which would give a tiny
    # negative p-value.
    p_value = min(1.0, max(0.0, p_value))

    alpha = 1.0 - confidence
    t_crit = t_ppf(1.0 - alpha / 2.0, df)
    diff = ma - mb
    margin = t_crit * se

    return {
        "t": t,
        "df": df,
        "p_value": p_value,
        "mean_a": ma,
        "mean_b": mb,
        "diff": diff,
        "se": se,
        "ci_low": diff - margin,
        "ci_high": diff + margin,
        "n_a": na,
        "n_b": nb,
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Standard normal quantile, by bisection (plenty accurate)."""
    lo, hi = -10.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def min_detectable_rel_diff(samples: Sequence[float], *, confidence: float = 0.95,
                            power: float = 0.8) -> float | None:
    """Smallest relative mean difference this sample size could detect.

    This is the honest counterpart to a p-value. With the default samples=20
    per cell the answer is typically 20-40%, which is *wider* than the 2%
    threshold compare.py used to judge regressions by — so those verdicts
    were noise, and a naive significance test would instead call everything
    "no difference". Reporting this number tells the operator how much
    resolution the run actually has.

    Returns a fraction (0.25 == a 25% change is the smallest detectable).
    """
    xs = [float(x) for x in samples if x is not None]
    if len(xs) < 2:
        return None
    mean, var = _mean_var(xs)
    if mean == 0.0 or var <= 0.0:
        return None
    se = math.sqrt(var / len(xs))
    mde = (t_ppf(1.0 - (1.0 - confidence) / 2.0, len(xs) - 1) + _norm_ppf(power)) * se
    return abs(mde / mean)


def required_n(mean: float, sd: float, rel_diff: float, *, confidence: float = 0.95,
               power: float = 0.8) -> int | None:
    """Per-group sample size needed to detect `rel_diff` (a fraction).

    Standard two-sample power formula. `sd` should be the observed standard
    deviation; `rel_diff` is e.g. 0.15 for "detect a 15% change".
    """
    if mean == 0.0 or sd <= 0.0 or rel_diff <= 0.0:
        return None
    effect = abs(mean * rel_diff)          # absolute shift we must detect
    n = 2.0 * ((_norm_ppf(1.0 - (1.0 - confidence) / 2.0) + _norm_ppf(power)) ** 2) \
        * (sd ** 2) / (effect ** 2)
    return int(math.ceil(n))


def two_proportion_ztest(x1: int, n1: int, x2: int, n2: int) -> dict | None:
    """Two-sided z-test on two proportions (used for error_rate).

    x1/n1 is the baseline (e.g. an earlier run), x2/n2 the current run.
    Returns None when either group is empty or both rates are identical.
    """
    if n1 <= 0 or n2 <= 0:
        return None
    p1, p2 = x1 / n1, x2 / n2
    if p1 == p2:
        return None
    pooled = (x1 + x2) / (n1 + n2)
    se2 = pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2)
    if se2 <= 0.0:
        return None
    se = math.sqrt(se2)
    z = (p2 - p1) / se
    p_value = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return {
        "z": z,
        "p_value": min(1.0, max(0.0, p_value)),
        "rate_a": p1,
        "rate_b": p2,
        "diff": p2 - p1,
    }

