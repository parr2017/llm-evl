"""Cross-run comparison: align cells by (target, prompt, concurrency) and
compute per-metric absolute/percentage deltas with an improve/regress verdict.

Pure functions on run dicts (as loaded from runs/run_<id>.json), no I/O,
so it is directly unit-testable.

Verdicts used to come from a fixed +/-2% percentage band, which at the
default samples=20 flags ordinary jitter as a regression. We now test the
per-request distributions with Welch's t-test (error_rate via a two-proportion
z-test) and only call a change real when it is both statistically significant
and large enough to matter. Each row also reports the minimum difference this
cell size could have detected, so "no change" is distinguishable from
"not enough resolution".
"""

from __future__ import annotations

from .stats import (
    min_detectable_rel_diff,
    required_n,
    two_proportion_ztest,
    welch_ttest,
)

# metric key -> (lower_is_better, human label)
METRICS: list[tuple[str, bool, str]] = [
    ("ttft_p50", True, "首字延迟 p50"),
    ("ttft_p90", True, "首字延迟 p90"),
    ("e2e_p50", True, "端到端 p50"),
    ("itl_p50", True, "逐 token 延迟 p50"),
    ("tokens_per_second_mean", False, "输出速度 tok/s"),
    ("error_rate", True, "错误率"),
]

# Metrics where 0 is a real, meaningful value rather than a "no data"
# sentinel. A TTFT of exactly 0 is impossible, so a 0 there means the cell
# has no data and the row is "na"; an error_rate of 0 means "was perfectly
# healthy" and must still get a verdict.
ZERO_IS_VALID: set[str] = {"error_rate"}

# metric key -> (RequestResult attribute, "scalar" | "list")
# Quantiles are order statistics, so the test runs on the raw per-request
# samples behind them instead.
SAMPLE_SPECS: dict[str, tuple[str, str]] = {
    "ttft_p50": ("ttft", "scalar"),
    "ttft_p90": ("ttft", "scalar"),
    "e2e_p50": ("e2e", "scalar"),
    "itl_p50": ("itl", "list"),
    "tokens_per_second_mean": ("tokens_per_second", "scalar"),
}

# Fallback when the test is undefined (tiny or zero-variance samples).
SAME_THRESHOLD_PCT = 2.0

# Significance level for the t-test / z-test.
DEFAULT_ALPHA = 0.05

# False-discovery-rate target for Benjamini-Hochberg across all comparisons in
# one report. Set to 0 to fall back to a raw per-test p < alpha.
DEFAULT_FDR = 0.05

# A statistically real but tiny change is not worth acting on, so require
# both significance and a practically meaningful magnitude.
MIN_PRACTICAL_PCT = 5.0


def _cell_key(cell: dict):
    return (cell.get("target"), cell.get("prompt_id"), cell.get("concurrency"))


def _run_meta(run: dict | None) -> dict | None:
    if run is None:
        return None
    return {
        "run_id": run.get("run_id"),
        "started_at": run.get("started_at"),
        "status": run.get("status"),
        "targets": [t.get("name") for t in run.get("targets", [])],
    }


def _samples(cell: dict, attr: str, kind: str) -> list[float]:
    """Per-request samples from a cell's successful requests."""
    out: list[float] = []
    for r in cell.get("requests") or []:
        if not r.get("ok"):
            continue
        v = r.get(attr)
        if kind == "list":
            out.extend(float(x) for x in (v or []) if x is not None)
        elif v is not None:
            out.append(float(v))
    return out


def _metric_diff(bv, ov, lower_better: bool, *,
                 test: dict | None = None,
                 alpha: float = DEFAULT_ALPHA,
                 min_practical_pct: float = MIN_PRACTICAL_PCT,
                 resolution: float | None = None,
                 needed_n: int | None = None,
                 zero_is_valid: bool = False) -> dict:
    """Build one metric's comparison row.

    `test` is the welch_ttest / two_proportion_ztest result, or None when the
    comparison cannot be tested (then we fall back to the legacy % band).
    """
    out = {
        "base": bv, "other": ov, "delta": None, "pct": None, "verdict": "na",
        "p_value": None, "significant": False,
        "ci_low": None, "ci_high": None,
        "resolution_pct": None if resolution is None else resolution * 100.0,
        "required_n": needed_n,
    }
    if bv is None or ov is None:
        return out

    if bv == 0 and not zero_is_valid:
        # Degenerate baseline (e.g. TTFT of exactly 0): treat as no data.
        return out

    # A zero baseline makes the percentage change undefined (and error_rate==0
    # is the common "was healthy" case), but the significance test is still
    # perfectly valid. Report the jump from zero as +100% so the practical
    # magnitude gate has something to work with.
    pct: float | None = None
    delta = ov - bv
    if bv != 0:
        pct = delta / abs(bv) * 100.0
    elif ov != 0:
        pct = 100.0 * (1 if ov > 0 else -1)
    out.update(delta=delta, pct=pct)

    if test is None:
        if pct is None or abs(pct) < SAME_THRESHOLD_PCT:
            out["verdict"] = "same"
        else:
            improved = (ov < bv) if lower_better else (ov > bv)
            out["verdict"] = "better" if improved else "worse"
        return out

    p_value = test.get("p_value")
    out["p_value"] = p_value
    out["significant"] = p_value is not None and p_value < alpha
    if "ci_low" in test:
        out["ci_low"] = test["ci_low"]
        out["ci_high"] = test["ci_high"]

    if out["significant"] and pct is not None and abs(pct) >= min_practical_pct:
        improved = (ov < bv) if lower_better else (ov > bv)
        out["verdict"] = "better" if improved else "worse"
    else:
        out["verdict"] = "same"
    return out



def compare_runs(base_run: dict, other_run: dict, *,
                 alpha: float = DEFAULT_ALPHA,
                 fdr: float = DEFAULT_FDR,
                 min_practical_pct: float = MIN_PRACTICAL_PCT) -> dict:
    """Compare two run dicts. Cells are aligned on (target, prompt_id,
    concurrency); only keys present in BOTH runs produce rows."""
    base_cells = {_cell_key(c): c for c in base_run.get("cells", [])}
    other_cells = {_cell_key(c): c for c in other_run.get("cells", [])}

    common = sorted(
        (k for k in base_cells if k in other_cells),
        key=lambda k: (k[0], k[2], k[1]),
    )

    rows: list[dict] = []
    pct_by_metric: dict[str, list[float]] = {name: [] for name, _, _ in METRICS}
    n_better = n_worse = 0
    tested_diffs: list[tuple[dict, float]] = []
    # Median MDE across tested cells: "this run resolves differences of ~X%".
    mdes: list[float] = []
    needed: list[int] = []

    for key in common:
        bc, oc = base_cells[key], other_cells[key]
        ba, oa = bc.get("aggregates", {}), oc.get("aggregates", {})
        metrics: dict[str, dict] = {}

        for name, lower_better, _label in METRICS:
            test = None
            resolution = None
            needed_n = None

            if name == "error_rate":
                test = two_proportion_ztest(
                    int(ba.get("n_errors") or 0), int(ba.get("n") or 0),
                    int(oa.get("n_errors") or 0), int(oa.get("n") or 0),
                )
            else:
                spec = SAMPLE_SPECS.get(name)
                if spec:
                    attr, kind = spec
                    bs = _samples(bc, attr, kind)
                    os_ = _samples(oc, attr, kind)
                    if bs and os_:
                        t = welch_ttest(bs, os_)
                        if t:
                            test = t
                            resolution = min_detectable_rel_diff(os_)
                            needed_n = required_n(
                                t["mean_b"], _sd(os_), min_practical_pct / 100.0,
                            )
                            if resolution is not None:
                                mdes.append(resolution)
                            if needed_n:
                                needed.append(needed_n)

            diff = _metric_diff(
                ba.get(name), oa.get(name), lower_better,
                test=test, alpha=alpha, min_practical_pct=min_practical_pct,
                resolution=resolution, needed_n=needed_n,
                zero_is_valid=name in ZERO_IS_VALID,
            )
            metrics[name] = diff
            if test is not None and test.get("p_value") is not None \
                    and diff["verdict"] != "na":
                tested_diffs.append((diff, test["p_value"]))
            if diff["pct"] is not None:
                pct_by_metric[name].append(diff["pct"])

        rows.append({
            "target": key[0],
            "prompt_id": key[1],
            "prompt_label": oc.get("prompt_label") or bc.get("prompt_label") or key[1],
            "bucket": oc.get("bucket") or bc.get("bucket"),
            "concurrency": key[2],
            "metrics": metrics,
        })

    # Multiplicity control: one report compares dozens of cell x metric pairs,
    # so significance is decided by BH at `fdr`, not by a raw p < alpha.
    n_tested = len(tested_diffs)
    n_sig = _apply_bh(tested_diffs, fdr) if fdr > 0 else sum(
        1 for d, _p in tested_diffs if d["p_value"] < alpha
    )

    # Re-derive verdicts from the final significance decision so counts can
    # never drift out of sync with the rows the user sees. Only rows that
    # actually ran a test are re-judged; rows without per-request samples keep
    # the legacy percentage verdict.
    tested_ids = {id(d) for d, _p in tested_diffs}
    n_better = n_worse = sig_better = sig_worse = 0
    for row in rows:
        for diff in row["metrics"].values():
            if id(diff) in tested_ids:
                material = (
                    diff["pct"] is not None
                    and abs(diff["pct"]) >= min_practical_pct
                )
                if not (diff["significant"] and material):
                    if diff["significant"]:
                        # Real but too small to act on — keep it visible.
                        diff["significant_but_negligible"] = True
                    diff["verdict"] = "same"
            if diff["verdict"] == "better":
                n_better += 1
                if diff["significant"]:
                    sig_better += 1
            elif diff["verdict"] == "worse":
                n_worse += 1
                if diff["significant"]:
                    sig_worse += 1

    summary = {
        name: (sum(vals) / len(vals) if vals else None)
        for name, vals in pct_by_metric.items()
    }

    return {
        "base": _run_meta(base_run),
        "other": _run_meta(other_run),
        "n_matched": len(rows),
        "n_base_only": len(base_cells) - len(common),
        "n_other_only": len(other_cells) - len(common),
        "n_better": n_better,
        "n_worse": n_worse,
        "n_tested": n_tested,
        "n_significant": n_sig,
        "n_significant_better": sig_better,
        "n_significant_worse": sig_worse,
        "alpha": alpha,
        "fdr": fdr,
        "min_practical_pct": min_practical_pct,
        "resolution_pct": (sorted(mdes)[len(mdes) // 2] * 100.0) if mdes else None,
        "required_n": (sorted(needed)[len(needed) // 2]) if needed else None,
        "summary": summary,
        "rows": rows,
    }


def _apply_bh(entries: list[tuple[dict, float]], fdr: float) -> int:
    """Benjamini-Hochberg step-up on the collected p-values.

    With ~50 cell x metric comparisons, testing each at alpha=0.05 yields
    ~2.5 false alarms even when nothing changed. BH controls the false
    discovery rate instead, at some cost in power. Marks `significant` on the
    diff dicts in place and returns how many survived.

    `entries` is a list of (diff_dict, p_value).
    """
    tested = [(d, p) for d, p in entries if p is not None]
    m = len(tested)
    if not m:
        return 0

    tested.sort(key=lambda dp: dp[1])
    # Largest k such that p_(k) <= k/m * fdr; everything at or below that
    # rank is significant.
    cutoff_rank = 0
    for i, (_d, p) in enumerate(tested, start=1):
        if p <= (i / m) * fdr:
            cutoff_rank = i
    n_sig = 0
    for i, (d, _p) in enumerate(tested, start=1):
        sig = i <= cutoff_rank
        d["significant"] = sig
        if sig:
            n_sig += 1
    return n_sig


def _sd(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5
