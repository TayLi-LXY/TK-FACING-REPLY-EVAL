#!/usr/bin/env python3
"""对比 v1/v2 测评结果 → compare.json。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from loop_lib import (
    DIM_NAMES,
    LoopPaths,
    load_xlsx_rows,
    read_state,
    resolve_loop_id,
    save_json,
    set_phase,
    summarize_eval_rows,
    write_state,
)


def cd_rate(dist: dict) -> float:
    total = sum(dist.values()) or 1
    return round(100.0 * (dist.get("C", 0) + dist.get("D", 0)) / total, 2)


def run_compare(loop_id: str = "") -> dict[str, Any]:
    loop_id = resolve_loop_id(loop_id)
    state = read_state(loop_id)
    paths = LoopPaths(loop_id)

    if not paths.v1_eval.exists() or not paths.v2_eval.exists():
        raise FileNotFoundError(
            f"需要 v1_eval 与 v2_eval 都存在: {paths.v1_eval} / {paths.v2_eval}"
        )

    _, rows1 = load_xlsx_rows(paths.v1_eval)
    _, rows2 = load_xlsx_rows(paths.v2_eval)
    s1 = summarize_eval_rows(rows1)
    s2 = summarize_eval_rows(rows2)
    state["stats"]["v1"] = s1
    state["stats"]["v2"] = s2

    dim_cmp = []
    for dim in DIM_NAMES:
        d1 = s1["dimension_grade_distribution"].get(dim, {})
        d2 = s2["dimension_grade_distribution"].get(dim, {})
        if not any(d1.values()) and not any(d2.values()):
            continue
        r1, r2 = cd_rate(d1), cd_rate(d2)
        dim_cmp.append({
            "dimension": dim,
            "v1_cd_rate": r1,
            "v2_cd_rate": r2,
            "delta_cd_rate": round(r2 - r1, 2),
            "improved": r2 < r1,
        })

    avg1, avg2 = s1.get("avg_score"), s2.get("avg_score")
    delta_avg = None
    if avg1 is not None and avg2 is not None:
        delta_avg = round(avg2 - avg1, 2)

    improved_dims = [d["dimension"] for d in dim_cmp if d["improved"]]
    regressed_dims = [d["dimension"] for d in dim_cmp if d["delta_cd_rate"] > 0]

    suggestion = (
        "ADOPTED"
        if (delta_avg is not None and delta_avg >= 0 and len(improved_dims) >= len(regressed_dims))
        else "ROLLED_BACK"
    )
    reason_parts = []
    if delta_avg is not None:
        reason_parts.append(f"均分 {avg1} → {avg2} (Δ{delta_avg:+})")
    reason_parts.append(f"C/D率改善维度 {len(improved_dims)}，退步 {len(regressed_dims)}")
    reason = "；".join(reason_parts)

    compare = {
        "loop_id": loop_id,
        "v1": {"avg_score": avg1, "scored_count": s1.get("scored_count")},
        "v2": {"avg_score": avg2, "scored_count": s2.get("scored_count")},
        "delta_avg": delta_avg,
        "dimensions": dim_cmp,
        "improved_dimensions": improved_dims,
        "regressed_dimensions": regressed_dims,
        "suggestion": suggestion,
        "suggestion_reason": reason,
    }
    save_json(paths.compare, compare)
    set_phase(state, "COMPARING")
    write_state(state)
    return compare


def main() -> None:
    parser = argparse.ArgumentParser(description="v1/v2 对比")
    parser.add_argument("--loop-id", default="")
    args = parser.parse_args()
    compare = run_compare(args.loop_id)
    print(json.dumps(compare, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
