"""Cross-run comparison: align cells by (target, prompt, concurrency) and
compute per-metric absolute/percentage deltas with an improve/regress verdict.

Pure functions on run dicts (as loaded from runs/run_<id>.json), no I/O,
so it is directly unit-testable.
"""

from __future__ import annotations

# metric key -> (lower_is_better, human label)
METRICS: list[tuple[str, bool, str]] = [
    ("ttft_p50", True, "首字延迟 p50"),
    ("ttft_p90", True, "首字延迟 p90"),
    ("e2e_p50", True, "端到端 p50"),
    ("itl_p50", True, "逐 token 延迟 p50"),
    ("tokens_per_second_mean", False, "输出速度 tok/s"),
    ("error_rate", True, "错误率"),
]

# Absolute percentage change below this (in points) counts as "same".
SAME_THRESHOLD_PCT = 2.0


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


def _metric_diff(bv, ov, lower_better: bool) -> dict:
    out = {"base": bv, "other": ov, "delta": None, "pct": None, "verdict": "na"}
    if bv is None or ov is None or bv == 0:
        return out
    delta = ov - bv
    pct = delta / abs(bv) * 100.0
    if abs(pct) < SAME_THRESHOLD_PCT:
        verdict = "same"
    else:
        improved = (ov < bv) if lower_better else (ov > bv)
        verdict = "better" if improved else "worse"
    out.update(delta=delta, pct=pct, verdict=verdict)
    return out


def compare_runs(base_run: dict, other_run: dict) -> dict:
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

    for key in common:
        bc, oc = base_cells[key], other_cells[key]
        ba, oa = bc.get("aggregates", {}), oc.get("aggregates", {})
        metrics: dict[str, dict] = {}
        for name, lower_better, _label in METRICS:
            diff = _metric_diff(ba.get(name), oa.get(name), lower_better)
            metrics[name] = diff
            if diff["pct"] is not None:
                pct_by_metric[name].append(diff["pct"])
            if diff["verdict"] == "better":
                n_better += 1
            elif diff["verdict"] == "worse":
                n_worse += 1
        rows.append({
            "target": key[0],
            "prompt_id": key[1],
            "prompt_label": oc.get("prompt_label") or bc.get("prompt_label") or key[1],
            "bucket": oc.get("bucket") or bc.get("bucket"),
            "concurrency": key[2],
            "metrics": metrics,
        })

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
        "summary": summary,
        "rows": rows,
    }
