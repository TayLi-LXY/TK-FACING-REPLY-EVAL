#!/usr/bin/env python3
"""终端交互式自进化闭环：门控在终端 input，不在 Cursor 对话框。

用法示例：
  python loop_run.py --dataset 测评集_抽样版.xlsx --scenes=5,18,27
  python loop_run.py --loop-id 20260904_171908          # 从当前阶段续跑
  python loop_run.py --loop-id ... --from-phase WAITING_ANNOTATION
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from loop_lib import (
    CONFIG_FILE,
    LoopPaths,
    SCRIPT_DIR,
    load_config,
    load_json,
    read_state,
    resolve_loop_id,
    save_json,
    set_phase,
)
from loop_optimize import auto_optimize_draft


def log(msg: str = "") -> None:
    print(msg, flush=True)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return raw if raw else default


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    ans = ask(f"{prompt} ({d})", "y" if default else "n").lower()
    if ans in ("y", "yes", "是", "确认", "ok", "好"):
        return True
    if ans in ("n", "no", "否", "取消"):
        return False
    return default


def run_cmd(args: list[str], check: bool = True) -> int:
    log(f"\n>> {' '.join(args)}")
    proc = subprocess.run(args, cwd=str(SCRIPT_DIR))
    if check and proc.returncode != 0:
        raise SystemExit(f"命令失败 (exit={proc.returncode}): {' '.join(args)}")
    return proc.returncode


def sync_runtime_model(paths: LoopPaths) -> None:
    if not CONFIG_FILE.exists():
        return
    cfg = load_config()
    if paths.runtime_config.exists():
        rc = load_json(paths.runtime_config)
    else:
        rc = dict(cfg)
    rc["model"] = cfg["model"]
    if paths.pe_v1.exists() and not (rc.get("agent") or {}).get("system_prompt"):
        pass
    save_json(paths.runtime_config, rc)


def batch_cmds(state: dict, version: str) -> tuple[list[str], list[str]]:
    paths = LoopPaths(state["loop_id"])
    pe = paths.pe_v1 if version == "v1" else paths.pe_v2
    reply = paths.v1_reply if version == "v1" else paths.v2_reply
    eval_out = paths.v1_eval if version == "v1" else paths.v2_eval
    dataset = SCRIPT_DIR / state["runs"].get(
        "dataset", f"loops/{state['loop_id']}/runs/dataset.xlsx"
    )
    scenes = state.get("scenes") or ""
    items = state.get("items") or ""

    run_args = [
        sys.executable, "run.py",
        "--input", str(dataset),
        "--output", str(reply),
        "--pe-file", str(pe),
        "--config", str(paths.runtime_config),
    ]
    eval_args = [
        sys.executable, "eval.py",
        "--input", str(reply),
        "--output", str(eval_out),
        "--config", str(paths.runtime_config),
    ]
    # 优先用 Workspace 固化副本，保证闭环可复现
    eval_prompt = paths.reports_dir / "eval_prompt.md"
    if eval_prompt.exists():
        eval_args += ["--eval-prompt", str(eval_prompt)]
    else:
        from loop_lib import resolve_prompt_path
        ep = resolve_prompt_path(state.get("scorer_path") or "")
        if ep.exists():
            eval_args += ["--eval-prompt", str(ep)]
    if scenes:
        run_args += ["--scenes", scenes]
        eval_args += ["--scenes", scenes]
    elif items:
        run_args += ["--items", items]
        eval_args += ["--items", items]
    else:
        run_args += ["--all"]
    return run_args, eval_args


def phase_running(state: dict, version: str) -> dict:
    paths = LoopPaths(state["loop_id"])
    sync_runtime_model(paths)
    log(f"\n===== {version.upper()} 跑批 =====")
    run_args, eval_args = batch_cmds(state, version)
    run_cmd(run_args)
    run_cmd(eval_args)
    mark = [
        sys.executable, "loop_ctl.py", "mark-batch",
        "--loop-id", state["loop_id"],
        "--version", version,
    ]
    if ask_yes_no("若有失败行，是否忽略并继续？", False):
        mark.append("--ignore-fail")
    run_cmd(mark)
    return read_state(state["loop_id"])


def phase_deep_diving(state: dict) -> dict:
    log("\n===== 深钻诊断 =====")
    run_cmd([
        sys.executable, "loop_analyze.py",
        "--loop-id", state["loop_id"],
        "--workers", "4",
    ], check=False)
    # analyze 失败时仍可能有 stats 兜底；再确保有 diagnosis
    paths = LoopPaths(state["loop_id"])
    if not paths.diagnosis.exists():
        log("诊断未生成，尝试仅合成/兜底...")
        run_cmd([
            sys.executable, "loop_analyze.py",
            "--loop-id", state["loop_id"],
            "--skip-synth",
        ], check=False)
    return read_state(state["loop_id"])


def phase_waiting_annotation(state: dict) -> dict:
    """终端门控：展示诊断并确认校准。"""
    paths = LoopPaths(state["loop_id"])
    log("\n===== 人工校准（终端门控）=====")
    if not paths.diagnosis.exists():
        raise SystemExit(f"缺少诊断报告: {paths.diagnosis}")

    diag = load_json(paths.diagnosis)
    oa = diag.get("overall_assessment") or {}
    log(f"均分: {diag.get('avg_score')}  样本: {diag.get('sample_count')}")
    log(f"总评: {oa.get('quality_diagnosis', '')}")
    log(f"瓶颈: {oa.get('key_bottleneck', '')}")
    log(f"系统问题: {oa.get('systemic_issue', '')}")
    opt = oa.get("optimization_priority") or []
    if not isinstance(opt, list):
        opt = []
    log("\n优化方向候选:")
    for i, item in enumerate(opt, 1):
        if isinstance(item, dict):
            log(f"  [{i}] {item.get('dimension') or item.get('rank')}: "
                f"{item.get('problem', '')} | 方向: {item.get('direction', '')}")
        else:
            log(f"  [{i}] {item}")

    log("\n操作说明:")
    log("  - 直接回车或输入 all : 采纳全部方向")
    log("  - 输入序号如 1,2,4     : 只采纳这些")
    log("  - 输入 n / quit       : 中止")
    choice = ask("请选择", "all").lower()
    if choice in ("n", "no", "quit", "exit", "否"):
        log("已取消，保持 WAITING_ANNOTATION。")
        raise SystemExit(0)

    if choice in ("", "all", "a", "y", "yes", "确认"):
        adopted = opt
        rejected: list = []
    else:
        idxs = set()
        for part in choice.replace("，", ",").split(","):
            part = part.strip()
            if part.isdigit():
                idxs.add(int(part))
        adopted = [opt[i - 1] for i in sorted(idxs) if 1 <= i <= len(opt)]
        rejected = [opt[i - 1] for i, _ in enumerate(opt, 1) if i not in idxs]

    summary = ask("业务总评语（可回车用诊断原文）", str(oa.get("quality_diagnosis") or ""))
    if not ask_yes_no(f"确认提交校准？（采纳 {len(adopted)} 条）", True):
        log("未确认，退出。")
        raise SystemExit(0)

    payload = {
        "business_summary": summary,
        "adopted_directions": adopted,
        "rejected_directions": rejected,
        "confirmed_via": "terminal",
    }
    tmp = paths.reports_dir / "_annotation_pending.json"
    save_json(tmp, payload)
    run_cmd([
        sys.executable, "loop_annotate.py",
        "--loop-id", state["loop_id"],
        "--from-file", str(tmp),
        "--confirmed",
    ])
    return read_state(state["loop_id"])


def phase_optimizing(state: dict) -> dict:
    """终端优化：自动改草稿或手工编辑后采纳。"""
    paths = LoopPaths(state["loop_id"])
    log("\n===== PE 优化（终端）=====")
    log(f"v1:    {paths.pe_v1}")
    log(f"draft: {paths.pe_draft}")

    mode = ask("优化方式: 1=LLM自动改草稿  2=我自己编辑 draft.md 后继续", "1")
    if mode.strip().startswith("2"):
        log(f"请编辑: {paths.pe_draft}")
        ask("编辑完成后按回车继续", "")
    else:
        try:
            auto_optimize_draft(paths, log=log)
        except RuntimeError as e:
            raise SystemExit(str(e)) from e

    log("\n--- draft 前 40 行预览 ---")
    draft = paths.pe_draft.read_text(encoding="utf-8") if paths.pe_draft.exists() else ""
    for line in draft.splitlines()[:40]:
        log(line)
    log("---")

    if not ask_yes_no("采纳该草稿为 v2 并开始 RUNNING_V2？", True):
        log("未采纳，保持 OPTIMIZING。可稍后: python loop_run.py --loop-id ...")
        raise SystemExit(0)

    run_cmd([
        sys.executable, "loop_ctl.py", "adopt-draft",
        "--loop-id", state["loop_id"],
    ])
    return read_state(state["loop_id"])


def phase_comparing(state: dict) -> dict:
    paths = LoopPaths(state["loop_id"])
    log("\n===== v1/v2 对比 =====")
    if not paths.compare.exists():
        run_cmd([sys.executable, "loop_compare.py", "--loop-id", state["loop_id"]])
    cmp = load_json(paths.compare)
    log(json.dumps({
        "v1": cmp.get("v1"),
        "v2": cmp.get("v2"),
        "delta_avg": cmp.get("delta_avg"),
        "suggestion": cmp.get("suggestion"),
        "suggestion_reason": cmp.get("suggestion_reason"),
        "improved_dimensions": cmp.get("improved_dimensions"),
        "regressed_dimensions": cmp.get("regressed_dimensions"),
    }, ensure_ascii=False, indent=2))

    log("\n===== 最终决策（终端门控）=====")
    sug = (cmp.get("suggestion") or "ADOPTED").upper()
    ans = ask("请决策: 1=ADOPTED采纳v2  2=ROLLED_BACK回滚", "1" if sug == "ADOPTED" else "2")
    if ans.strip().startswith("2") or ans.upper().startswith("R"):
        decision = "ROLLED_BACK"
    else:
        decision = "ADOPTED"
    note = ask("备注（可空）", "")
    if not ask_yes_no(f"确认执行 {decision}？", True):
        log("未确认，保持 COMPARING。")
        raise SystemExit(0)

    run_cmd([
        sys.executable, "loop_decide.py",
        "--loop-id", state["loop_id"],
        "--decision", decision,
        "--note", note,
        "--confirmed",
    ])
    return read_state(state["loop_id"])


def run_loop(state: dict, stop_after: str = "") -> None:
    """按阶段推进直到 DONE 或 stop_after。"""
    while True:
        state = read_state(state["loop_id"])
        phase = state["phase"]
        log(f"\n######## 当前阶段: {phase} ########")

        if phase == "DONE":
            log("闭环完成。")
            log(json.dumps(state.get("decision"), ensure_ascii=False, indent=2))
            break
        if phase == "FAILED":
            log("存在失败 case。请修复后: python loop_ctl.py set-phase --phase RUNNING_V1")
            log("或 mark-batch --ignore-fail 后继续。")
            break
        if phase == "IDLE":
            set_phase(state, "RUNNING_V1")
            continue
        if phase == "RUNNING_V1":
            state = phase_running(state, "v1")
        elif phase == "DEEP_DIVING":
            state = phase_deep_diving(state)
        elif phase == "WAITING_ANNOTATION":
            state = phase_waiting_annotation(state)
        elif phase == "OPTIMIZING":
            state = phase_optimizing(state)
        elif phase == "RUNNING_V2":
            state = phase_running(state, "v2")
        elif phase == "COMPARING":
            state = phase_comparing(state)
        else:
            raise SystemExit(f"未知阶段: {phase}")

        if stop_after and read_state(state["loop_id"])["phase"] == stop_after:
            log(f"已到达 --stop-after {stop_after}，暂停。")
            break


def main() -> None:
    parser = argparse.ArgumentParser(
        description="终端交互式自进化闭环（门控在终端）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--loop-id", default="", help="续跑已有 loop；空则 start 新 loop")
    parser.add_argument("--dataset", default="测评集_抽样版.xlsx")
    parser.add_argument("--scenes", default="")
    parser.add_argument("--items", default="")
    parser.add_argument("--force-start", action="store_true", help="强制新建同名 loop")
    parser.add_argument(
        "--from-phase",
        default="",
        help="强制把当前 loop 设到该阶段再跑（如 WAITING_ANNOTATION）",
    )
    parser.add_argument(
        "--stop-after",
        default="",
        help="到达该阶段后暂停（阶段名）",
    )
    args = parser.parse_args()

    if not CONFIG_FILE.exists():
        raise SystemExit("缺少 config.json，请先从 config.example.json 复制并填写密钥")

    if args.loop_id:
        loop_id = args.loop_id
        state = read_state(loop_id)
    elif not args.loop_id and (SCRIPT_DIR / "loops").exists():
        # 无 --loop-id：询问新建还是续跑最新
        try:
            latest = resolve_loop_id(None)
            log(f"发现最新 loop: {latest}")
            if ask_yes_no("续跑该 loop？选 n 则新建", True):
                state = read_state(latest)
            else:
                state = None  # type: ignore
        except SystemExit:
            state = None  # type: ignore
    else:
        state = None  # type: ignore

    if state is None:
        start_args = [
            sys.executable, "loop_ctl.py", "start",
            "--dataset", args.dataset,
        ]
        if args.scenes:
            start_args += ["--scenes", args.scenes]
        if args.items:
            start_args += ["--items", args.items]
        if args.force_start:
            start_args += ["--force"]
        run_cmd(start_args)
        state = read_state(resolve_loop_id(None))

    if args.from_phase:
        set_phase(state, args.from_phase)
        state = read_state(state["loop_id"])

    sync_runtime_model(LoopPaths(state["loop_id"]))
    log(f"Loop: {state['loop_id']}  phase={state['phase']}")
    log("提示: 人工确认都会在本终端询问，请勿关闭窗口。")
    run_loop(state, stop_after=args.stop_after)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n已中断。可用 python loop_run.py --loop-id <id> 续跑。")
        raise SystemExit(130)
