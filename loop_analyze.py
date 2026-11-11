#!/usr/bin/env python3
"""深钻诊断：按维度 MapReduce 分析测评结果 → diagnosis.json。"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from loop_lib import (
    ANALYZER_DIM_PE,
    ANALYZER_SYNTH_PE,
    DIM_NAMES,
    HARD_DIMS,
    LoopPaths,
    call_llm,
    extract_json,
    load_config,
    load_xlsx_rows,
    make_openai_client,
    read_state,
    render_template,
    resolve_loop_id,
    save_json,
    set_phase,
    summarize_eval_rows,
    write_state,
)


def load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def grade_of(row: dict, dim: str) -> str:
    return (row.get(f"{dim}_分") or "N/A").strip().upper() or "N/A"


def build_case_brief(row: dict, seq: int) -> dict:
    grades = {d: grade_of(row, d) for d in DIM_NAMES if row.get(f"{d}_分")}
    return {
        "seq": seq,
        "instance_id": row.get("instance_id", ""),
        "title": row.get("title", ""),
        "score": row.get("综合得分", ""),
        "grades": grades,
        "chapter": row.get("target", ""),
        "knowledge_point": row.get("title", ""),
    }


def build_case_full(row: dict, seq: int) -> dict:
    reasons = {
        d: row.get(f"{d}_理由", "")
        for d in DIM_NAMES
        if row.get(f"{d}_分")
    }
    return {
        "seq": seq,
        "case_input": {
            "instance_id": row.get("instance_id", ""),
            "title": row.get("title", ""),
            "target": row.get("target", ""),
            "background": row.get("background", "")[:500],
            "current_hwp_message": row.get("current_hwp_message", ""),
            "customer_personality": row.get("customer_personality", "")[:300],
        },
        "agent_output": row.get("生产回复", ""),
        "score": row.get("综合得分", ""),
        "grades": {d: grade_of(row, d) for d in DIM_NAMES if row.get(f"{d}_分")},
        "reasons": reasons,
        "score_note": row.get("得分说明", ""),
    }


def dist_percent(dist: dict[str, int]) -> dict:
    total = sum(dist.values()) or 1
    return {
        g: {"count": dist.get(g, 0), "percentage": round(100 * dist.get(g, 0) / total, 1)}
        for g in ("A", "B", "C", "D", "N/A")
    }


def analyze_dimension(
    dim: str,
    rows: list[dict],
    dist_raw: dict[str, int],
    client,
    model_cfg: dict,
    dim_prompt: str,
    retries: int,
) -> dict:
    low = []
    high = []
    for i, row in enumerate(rows, 1):
        g = grade_of(row, dim)
        if g in ("C", "D"):
            low.append({
                "seq": i,
                "score": row.get("综合得分", ""),
                "agent_output": row.get("生产回复", ""),
                "grade": g,
                "reason": row.get(f"{dim}_理由", ""),
                "case_input": {
                    "title": row.get("title", ""),
                    "current_hwp_message": row.get("current_hwp_message", ""),
                },
            })
        elif g in ("A", "B") and len(high) < 3:
            high.append({
                "seq": i,
                "agent_output": row.get("生产回复", ""),
                "grade": g,
                "reason": row.get(f"{dim}_理由", ""),
            })

    # 无 C/D 时仍给结构化空结果，跳过 LLM
    if not low:
        return {
            "dimension_name": dim,
            "violation_level": "HARD" if dim in HARD_DIMS else "SOFT",
            "grade_distribution": dist_percent(dist_raw),
            "high_score_characteristics": {
                "summary": "本维度无 C/D 低分样本",
                "details": "样本均在 A/B 或未评分",
                "rule_references": [],
            },
            "low_score_problems": {
                "summary": "无低分问题",
                "details": "",
                "rule_references": [],
            },
            "improvement_direction": "保持现状，关注其他瓶颈维度",
            "priority": 99,
            "priority_reason": "无 C/D",
        }

    ctx = {
        "dimension_name": dim,
        "violation_level": "HARD" if dim in HARD_DIMS else "SOFT",
        "grade_distribution": dist_percent(dist_raw),
        "low_score_cases": low[:30],
        "high_score_samples": high,
    }
    user = render_template(
        "输入如下 dimension_context JSON：\n{{dimension_context}}\n\n请按系统要求输出严格 JSON。",
        {"dimension_context": json.dumps(ctx, ensure_ascii=False)},
    )
    # agents/analyzer/dimension.md 本身就是 system
    result = call_llm(
        client,
        model_cfg["modelName"],
        [
            {"role": "system", "content": dim_prompt},
            {"role": "user", "content": user},
        ],
        {**model_cfg, "temperature": min(float(model_cfg.get("temperature", 0.7)), 0.3)},
        retries=retries,
    )
    if not result["success"]:
        return {
            "dimension_name": dim,
            "violation_level": "HARD" if dim in HARD_DIMS else "SOFT",
            "grade_distribution": dist_percent(dist_raw),
            "error": result.get("error"),
            "improvement_direction": "分析失败，请重试",
            "priority": 50,
            "priority_reason": "LLM 调用失败",
        }
    try:
        obj = extract_json(result["reply"])
        obj.setdefault("dimension_name", dim)
        obj.setdefault("grade_distribution", dist_percent(dist_raw))
        return obj
    except Exception as e:
        return {
            "dimension_name": dim,
            "violation_level": "HARD" if dim in HARD_DIMS else "SOFT",
            "grade_distribution": dist_percent(dist_raw),
            "raw": result["reply"][:2000],
            "error": f"解析失败: {e}",
            "improvement_direction": "解析失败，请重试",
            "priority": 50,
            "priority_reason": "JSON 解析失败",
        }


def synthesize(
    state: dict,
    stats: dict,
    dim_analyses: list[dict],
    rows: list[dict],
    client,
    model_cfg: dict,
    synth_prompt: str,
    retries: int,
) -> dict:
    scored = []
    for i, row in enumerate(rows, 1):
        if row.get("综合得分"):
            scored.append((i, row, float(row["综合得分"]) if str(row["综合得分"]).replace(".", "", 1).isdigit() or True else 0))
    # sort by score
    def score_val(r):
        try:
            return float(r.get("综合得分") or 0)
        except ValueError:
            return 0.0

    indexed = [(i, row) for i, row in enumerate(rows, 1) if row.get("主题相关性_分")]
    high = sorted(indexed, key=lambda x: score_val(x[1]), reverse=True)[:3]
    low = sorted(indexed, key=lambda x: score_val(x[1]))[:5]
    errors = [
        build_case_brief(row, i)
        for i, row in enumerate(rows, 1)
        if row.get("执行结果") and row.get("执行结果") != "成功" and not row.get("主题相关性_分")
    ]

    report_context = {
        "run": {
            "run_no": state["loop_id"],
            "target_agent": "facing_reply_gen",
            "scorer": "agents/scorer",
            "dataset": state.get("dataset"),
        },
        "stats": {
            "avg_score": stats.get("avg_score"),
            "scored_count": stats.get("scored_count"),
            "dimension_grade_distribution": stats.get("dimension_grade_distribution"),
        },
        "dimension_analyses": dim_analyses,
        "high_score_cases": [build_case_full(r, i) for i, r in high],
        "low_score_cases": [build_case_full(r, i) for i, r in low],
        "error_cases": errors,
        "all_cases_brief": [build_case_brief(row, i) for i, row in indexed],
    }

    # 压缩维度分析结果，降低合成输出截断/坏 JSON 概率
    slim_dims = []
    for d in dim_analyses:
        slim_dims.append({
            "dimension_name": d.get("dimension_name"),
            "violation_level": d.get("violation_level"),
            "grade_distribution": d.get("grade_distribution"),
            "high_score_characteristics": {
                "summary": (d.get("high_score_characteristics") or {}).get("summary", "")
            },
            "low_score_problems": {
                "summary": (d.get("low_score_problems") or {}).get("summary", "")
            },
            "improvement_direction": d.get("improvement_direction"),
            "priority": d.get("priority"),
            "priority_reason": d.get("priority_reason"),
        })
    report_context["dimension_analyses"] = slim_dims

    user = render_template(
        "输入如下 report_context JSON：\n{{report_context}}\n\n请按系统要求输出严格 JSON，字段值避免未转义引号。",
        {"report_context": json.dumps(report_context, ensure_ascii=False)},
    )
    last_err = None
    for attempt in range(max(retries, 1) + 1):
        result = call_llm(
            client,
            model_cfg["modelName"],
            [
                {"role": "system", "content": synth_prompt},
                {"role": "user", "content": user},
            ],
            {**model_cfg, "temperature": 0.2,
             "maxTokens": max(int(model_cfg.get("maxTokens") or 2048), 8192)},
            retries=1,
        )
        if not result["success"]:
            last_err = result.get("error")
            continue
        try:
            obj = extract_json(result["reply"])
            if not obj.get("dimensions_analysis"):
                obj["dimensions_analysis"] = dim_analyses
            return obj
        except Exception as e:
            last_err = str(e)
            print(f"  [warn] 合成 JSON 解析失败 attempt={attempt+1}: {e}")
    print(f"  [warn] 合成失败，使用维度分析兜底: {last_err}")
    return fallback_report(stats, dim_analyses)


def fallback_report(stats: dict, dim_analyses: list[dict]) -> dict:
    ranked = sorted(
        [d for d in dim_analyses if isinstance(d, dict)],
        key=lambda x: x.get("priority") if isinstance(x.get("priority"), int) else 999,
    )
    top = ranked[:5]
    opt = []
    for i, d in enumerate(top, 1):
        opt.append({
            "rank": i,
            "dimension": d.get("dimension_name"),
            "problem": (d.get("low_score_problems") or {}).get("summary") or d.get("priority_reason") or "",
            "direction": d.get("improvement_direction") or "",
            "estimated_impact": "待人工评估",
        })
    return {
        "knowledge_point": "all",
        "sample_count": stats.get("scored_count"),
        "avg_score": stats.get("avg_score"),
        "dimensions_analysis": dim_analyses,
        "cross_dimension_analysis": {"case_profiles": [], "dimension_correlations": []},
        "overall_assessment": {
            "quality_diagnosis": f"小范围评测均分 {stats.get('avg_score')}，共 {stats.get('scored_count')} 条已评分样本。",
            "key_bottleneck": (top[0].get("dimension_name") if top else ""),
            "systemic_issue": "由各维度 Map 分析结果汇总（合成器解析失败时的兜底）。",
            "optimization_priority": opt,
            "risk_notes": [
                "改硬违规维度规则时避免过度限制，以免损伤主题相关性与口语自然度。"
            ],
        },
        "_synth_fallback": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成深钻诊断报告")
    parser.add_argument("--loop-id", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--dims", default="", help="仅分析指定维度，逗号分隔；默认全量")
    parser.add_argument("--skip-synth", action="store_true", help="只做维度分析，不做合成")
    parser.add_argument("--synth-only", action="store_true",
                        help="跳过维度分析，复用 reports/dim_analyses.json 只做合成")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    loop_id = resolve_loop_id(args.loop_id)
    state = read_state(loop_id)
    paths = LoopPaths(loop_id)
    if not paths.v1_eval.exists():
        raise SystemExit(f"缺少 v1 测评: {paths.v1_eval}")

    set_phase(state, "DEEP_DIVING")
    _, rows = load_xlsx_rows(paths.v1_eval)
    stats = summarize_eval_rows(rows)
    state["stats"]["v1"] = stats
    write_state(state)

    dims = [d.strip() for d in args.dims.split(",") if d.strip()] if args.dims else list(DIM_NAMES)
    # 只保留表里实际有分的维度
    dims = [d for d in dims if any(row.get(f"{d}_分") for row in rows)]
    if args.dry_run:
        print(json.dumps({"loop_id": loop_id, "dims": dims, "stats": stats}, ensure_ascii=False, indent=2))
        return

    cfg = load_config(paths.runtime_config if paths.runtime_config.exists() else None)
    model_cfg = cfg["model"]
    client = make_openai_client(model_cfg)
    dim_prompt = load_prompt(ANALYZER_DIM_PE)
    synth_prompt = load_prompt(ANALYZER_SYNTH_PE)

    dist_all = stats["dimension_grade_distribution"]
    dim_cache = paths.reports_dir / "dim_analyses.json"
    order = {d: i for i, d in enumerate(DIM_NAMES)}

    if args.synth_only:
        if not dim_cache.exists():
            raise SystemExit(f"缺少维度缓存: {dim_cache}，请先完整跑 loop_analyze 或去掉 --synth-only")
        dim_analyses = json.loads(dim_cache.read_text(encoding="utf-8"))
        print(f"  复用维度分析缓存: {len(dim_analyses)} 维")
    else:
        dim_analyses = []
        print(f"  维度分析: {len(dims)} 维, workers={args.workers}")

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {
                ex.submit(
                    analyze_dimension,
                    dim,
                    rows,
                    dist_all.get(dim, {"A": 0, "B": 0, "C": 0, "D": 0, "N/A": 0}),
                    client,
                    model_cfg,
                    dim_prompt,
                    args.retries,
                ): dim
                for dim in dims
            }
            for fut in as_completed(futs):
                dim = futs[fut]
                try:
                    obj = fut.result()
                    dim_analyses.append(obj)
                    print(f"  [ok] {dim}")
                except Exception as e:
                    print(f"  [fail] {dim}: {e}")
                    dim_analyses.append({"dimension_name": dim, "error": str(e)})

        dim_analyses.sort(key=lambda x: order.get(x.get("dimension_name", ""), 999))
        save_json(dim_cache, dim_analyses)

    if args.skip_synth:
        report = fallback_report(stats, dim_analyses)
    else:
        print("  合成综合报告...")
        report = synthesize(state, stats, dim_analyses, rows, client, model_cfg, synth_prompt, args.retries)

    report["_meta"] = {
        "loop_id": loop_id,
        "source_eval": str(paths.v1_eval),
        "stats": stats,
    }
    save_json(paths.diagnosis, report)
    set_phase(state, "WAITING_ANNOTATION")
    print(json.dumps({
        "ok": True,
        "loop_id": loop_id,
        "phase": "WAITING_ANNOTATION",
        "diagnosis": str(paths.diagnosis),
        "avg_score": stats.get("avg_score"),
        "overall_assessment_keys": list((report.get("overall_assessment") or {}).keys()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
