#!/usr/bin/env python3
"""闭环控制：start / status / set-phase / mark-batch / adopt-draft。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from loop_lib import (
    PHASES,
    LoopPaths,
    SCRIPT_DIR,
    default_state,
    init_memory,
    load_config,
    load_xlsx_rows,
    new_loop_id,
    read_state,
    resolve_loop_id,
    set_phase,
    summarize_eval_rows,
    write_runtime_config,
    write_state,
)


def start_loop(
    dataset: str = "测评集_抽样版.xlsx",
    loop_id: str = "",
    scenes: str = "",
    items: str = "",
    pe_file: str = "",
    force: bool = False,
    *,
    agent_id: str = "",
    agent_name: str = "",
    scorer: str = "",
    scorer_id: str = "",
) -> dict[str, Any]:
    """创建 loop workspace，phase=RUNNING_V1。返回结果 dict。"""
    loop_id = loop_id or new_loop_id()
    paths = LoopPaths(loop_id)
    if paths.state_file.exists() and not force:
        raise FileExistsError(f"loop 已存在: {loop_id}（加 force 可覆盖重建）")

    dataset_path = Path(dataset)
    if not dataset_path.is_absolute():
        dataset_path = SCRIPT_DIR / dataset_path
    if not dataset_path.exists():
        raise FileNotFoundError(f"数据集不存在: {dataset_path}")

    cfg = load_config()
    pe_text = cfg["agent"]["system_prompt"]
    resolved_agent_name = agent_name or cfg["agent"].get("agent_name") or "config"
    resolved_agent_id = agent_id or cfg["agent"].get("code") or "config"
    from loop_lib import SCORER_PE, resolve_prompt_path

    if pe_file:
        pe_path = resolve_prompt_path(pe_file)
        if not pe_path.exists():
            raise FileNotFoundError(f"智能体 PE 不存在: {pe_path}")
        pe_text = pe_path.read_text(encoding="utf-8")
        if not agent_name:
            resolved_agent_name = pe_path.stem

    scorer_path = resolve_prompt_path(scorer) if scorer else SCORER_PE
    if not scorer_path.exists():
        raise FileNotFoundError(f"评分器提示词不存在: {scorer_path}")
    try:
        scorer_rel = str(scorer_path.relative_to(SCRIPT_DIR)).replace("\\", "/")
    except ValueError:
        scorer_rel = str(scorer_path)

    paths.ensure_dirs()
    paths.pe_v1.write_text(pe_text, encoding="utf-8")
    paths.pe_draft.write_text(pe_text, encoding="utf-8")
    # 闭环内固化评分器副本，保证可复现
    scorer_copy = paths.reports_dir / "eval_prompt.md"
    shutil.copy2(scorer_path, scorer_copy)
    write_runtime_config(paths, pe_text, cfg)
    init_memory(paths, str(dataset_path))

    dataset_copy = paths.runs_dir / "dataset.xlsx"
    shutil.copy2(dataset_path, dataset_copy)

    try:
        dataset_rel = str(dataset_path.relative_to(SCRIPT_DIR))
    except ValueError:
        dataset_rel = str(dataset_path)
    state = default_state(
        loop_id,
        dataset=dataset_rel,
        scenes=scenes or "",
        items=items or "",
        agent_id=resolved_agent_id,
        agent_name=resolved_agent_name,
        scorer_id=scorer_id or scorer_path.stem,
        scorer_path=scorer_rel,
    )
    state["phase"] = "RUNNING_V1"
    state["runs"]["dataset"] = (
        dataset_copy.relative_to(SCRIPT_DIR).as_posix()
    )
    state["reports"]["eval_prompt"] = (
        scorer_copy.relative_to(SCRIPT_DIR).as_posix()
    )
    write_state(state)

    return {
        "ok": True,
        "loop_id": loop_id,
        "phase": state["phase"],
        "paths": {
            "root": str(paths.root),
            "state": str(paths.state_file),
            "pe_v1": str(paths.pe_v1),
            "dataset": str(dataset_copy),
        },
        "next_batch_hint": _batch_cmd(state, version="v1"),
    }


def get_status(loop_id: str = "") -> dict[str, Any]:
    loop_id = resolve_loop_id(loop_id)
    state = read_state(loop_id)
    paths = LoopPaths(loop_id)
    extras = {
        "files": {
            "diagnosis": paths.diagnosis.exists(),
            "annotation": paths.annotation.exists(),
            "compare": paths.compare.exists(),
            "v1_reply": paths.v1_reply.exists(),
            "v1_eval": paths.v1_eval.exists(),
            "v2_reply": paths.v2_reply.exists(),
            "v2_eval": paths.v2_eval.exists(),
            "draft": paths.pe_draft.exists(),
            "v2_pe": paths.pe_v2.exists(),
        }
    }
    return {**state, **extras}


def mark_batch(
    loop_id: str = "",
    version: str = "v1",
    ignore_fail: bool = False,
) -> dict[str, Any]:
    loop_id = resolve_loop_id(loop_id)
    state = read_state(loop_id)
    paths = LoopPaths(loop_id)
    eval_path = paths.v1_eval if version == "v1" else paths.v2_eval
    reply_path = paths.v1_reply if version == "v1" else paths.v2_reply

    if not reply_path.exists():
        raise FileNotFoundError(f"回复文件不存在: {reply_path}")
    if not eval_path.exists():
        raise FileNotFoundError(f"测评文件不存在: {eval_path}（请先跑完 eval.py）")

    _, rows = load_xlsx_rows(eval_path)
    stats = summarize_eval_rows(rows)
    fail_count = 0
    unscored_count = 0
    for row in rows:
        status = (row.get("执行结果") or "").strip()
        scored = bool((row.get("主题相关性_分") or "").strip())
        if status == "成功":
            if not scored:
                unscored_count += 1
        elif status or not scored:
            # 执行失败，或完全没跑出来
            fail_count += 1
    stats["batch_fail_rows"] = fail_count
    stats["batch_unscored_rows"] = unscored_count
    state["stats"][version] = stats

    # 缺打分一律拦住：常见原因是 DashScope 欠费，忽略失败会误推进到诊断
    if unscored_count > 0:
        set_phase(state, "FAILED")
        return {
            "ok": False,
            "phase": "FAILED",
            "fail_count": fail_count,
            "unscored_count": unscored_count,
            "stats": stats,
            "hint": f"有 {unscored_count} 条回复成功但未打分（请检查 API Key/账户是否欠费），点「重跑未成功」补跑",
        }

    if fail_count > 0 and not ignore_fail:
        set_phase(state, "FAILED")
        return {
            "ok": False,
            "phase": "FAILED",
            "fail_count": fail_count,
            "stats": stats,
            "hint": "请修复失败 case 后重跑，或 ignore_fail 继续",
        }

    if version == "v1":
        set_phase(state, "DEEP_DIVING")
    else:
        set_phase(state, "COMPARING")
    return {
        "ok": True,
        "loop_id": loop_id,
        "phase": state["phase"],
        "stats": stats,
    }


def adopt_draft(loop_id: str = "") -> dict[str, Any]:
    """采纳 PE 草稿为 v2，并进入 RUNNING_V2。"""
    loop_id = resolve_loop_id(loop_id)
    state = read_state(loop_id)
    paths = LoopPaths(loop_id)
    if not paths.pe_draft.exists():
        raise FileNotFoundError(f"草稿不存在: {paths.pe_draft}")
    text = paths.pe_draft.read_text(encoding="utf-8")
    paths.pe_v2.write_text(text, encoding="utf-8")
    write_runtime_config(paths, text)
    state["pe"]["active"] = "v2"
    set_phase(state, "RUNNING_V2")
    return {
        "ok": True,
        "loop_id": loop_id,
        "phase": "RUNNING_V2",
        "pe_v2": str(paths.pe_v2),
        "next": _batch_cmd(state, "v2"),
    }


def list_loops() -> list[dict[str, Any]]:
    from loop_lib import LOOPS_DIR

    if not LOOPS_DIR.exists():
        return []
    items = []
    for p in sorted(LOOPS_DIR.iterdir()):
        sf = p / "state.json"
        if sf.exists():
            st = json.loads(sf.read_text(encoding="utf-8"))
            items.append({
                "loop_id": p.name,
                "phase": st.get("phase"),
                "updated_at": st.get("updated_at"),
            })
    return items


def _batch_cmd(state: dict, version: str) -> str:
    paths = LoopPaths(state["loop_id"])
    pe = paths.pe_v1 if version == "v1" else paths.pe_v2
    reply = paths.v1_reply if version == "v1" else paths.v2_reply
    eval_out = paths.v1_eval if version == "v1" else paths.v2_eval
    dataset = SCRIPT_DIR / state["runs"].get(
        "dataset", f"loops/{state['loop_id']}/runs/dataset.xlsx"
    )
    scenes = state.get("scenes") or ""
    items = state.get("items") or ""
    filter_args = ""
    if scenes:
        filter_args += f' --scenes="{scenes}"'
    elif items:
        filter_args += f' --items="{items}"'
    else:
        filter_args += " --all"

    run_cmd = (
        f'python run.py --input "{dataset}" --output "{reply}" '
        f'--pe-file "{pe}" --config "{paths.runtime_config}"{filter_args}'
    )
    eval_cmd = (
        f'python eval.py --input "{reply}" --output "{eval_out}" '
        f'--config "{paths.runtime_config}"'
    )
    if scenes:
        eval_cmd += f' --scenes="{scenes}"'
    if items:
        eval_cmd += f' --items="{items}"'
    return (
        f"{run_cmd}\n然后:\n{eval_cmd}\n完成后:\n"
        f"python loop_ctl.py mark-batch --loop-id {state['loop_id']} --version {version}"
    )


def cmd_start(args: argparse.Namespace) -> None:
    result = start_loop(
        dataset=args.dataset,
        loop_id=args.loop_id or "",
        scenes=args.scenes or "",
        items=args.items or "",
        pe_file=args.pe_file or "",
        force=args.force,
    )
    print(json.dumps({
        "ok": result["ok"],
        "loop_id": result["loop_id"],
        "phase": result["phase"],
        "paths": result["paths"],
    }, ensure_ascii=False, indent=2))
    print("\n下一步（RUNNING_V1）:")
    print(result["next_batch_hint"])


def cmd_status(args: argparse.Namespace) -> None:
    out = get_status(args.loop_id)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    phase = out["phase"]
    loop_id = out["loop_id"]
    if phase == "RUNNING_V1":
        print("\n提示: " + _batch_cmd(out, "v1").split("\n")[0])
    elif phase == "RUNNING_V2":
        print("\n提示: " + _batch_cmd(out, "v2").split("\n")[0])
    elif phase == "DEEP_DIVING":
        print(f"\n下一步: python loop_analyze.py --loop-id {loop_id}")
    elif phase == "WAITING_ANNOTATION":
        print(f"\n下一步: 与用户确认后 python loop_annotate.py --loop-id {loop_id} --from-file ...")
    elif phase == "OPTIMIZING":
        print(f"\n下一步: 切换优化器 Skill；python loop_optimize_io.py read --loop-id {loop_id}")
    elif phase == "COMPARING":
        print(f"\n下一步: python loop_compare.py --loop-id {loop_id}")


def cmd_set_phase(args: argparse.Namespace) -> None:
    loop_id = resolve_loop_id(args.loop_id)
    state = read_state(loop_id)
    set_phase(state, args.phase)
    print(json.dumps({"ok": True, "loop_id": loop_id, "phase": args.phase}, ensure_ascii=False))


def cmd_mark_batch(args: argparse.Namespace) -> None:
    result = mark_batch(args.loop_id, args.version, args.ignore_fail)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_adopt_draft(args: argparse.Namespace) -> None:
    result = adopt_draft(args.loop_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_list(_: argparse.Namespace) -> None:
    print(json.dumps(list_loops(), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="自进化闭环控制")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="启动闭环")
    p_start.add_argument("--dataset", default="测评集_抽样版.xlsx", help="测评集路径")
    p_start.add_argument("--loop-id", default="", help="指定 loop_id，默认时间戳")
    p_start.add_argument("--scenes", default="", help="传给 run/eval 的 scenes")
    p_start.add_argument("--items", default="", help="传给 run/eval 的 items")
    p_start.add_argument("--pe-file", default="", help="初始 PE 文件，默认读 config.json")
    p_start.add_argument("--force", action="store_true")
    p_start.set_defaults(func=cmd_start)

    p_status = sub.add_parser("status", help="查询状态")
    p_status.add_argument("--loop-id", default="")
    p_status.set_defaults(func=cmd_status)

    p_phase = sub.add_parser("set-phase", help="手动设置阶段")
    p_phase.add_argument("--loop-id", default="")
    p_phase.add_argument("--phase", required=True, choices=PHASES)
    p_phase.set_defaults(func=cmd_set_phase)

    p_mark = sub.add_parser("mark-batch", help="标记 v1/v2 跑批完成")
    p_mark.add_argument("--loop-id", default="")
    p_mark.add_argument("--version", required=True, choices=["v1", "v2"])
    p_mark.add_argument("--ignore-fail", action="store_true")
    p_mark.set_defaults(func=cmd_mark_batch)

    p_adopt = sub.add_parser("adopt-draft", help="采纳草稿为 v2 并进入 RUNNING_V2")
    p_adopt.add_argument("--loop-id", default="")
    p_adopt.set_defaults(func=cmd_adopt_draft)

    p_list = sub.add_parser("list", help="列出全部 loop")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
