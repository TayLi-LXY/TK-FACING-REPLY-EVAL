"""Web orchestrator: auto-advance loop phases until HITL gates."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from loop_annotate import submit_annotation
from loop_compare import run_compare
from loop_ctl import adopt_draft, mark_batch, start_loop
from loop_decide import apply_decision
from loop_lib import (
    CONFIG_FILE,
    SCRIPT_DIR,
    LoopPaths,
    load_config,
    load_json,
    load_xlsx_rows,
    read_state,
    save_json,
    set_phase,
)
from loop_optimize import auto_optimize_draft

from .jobs import get_or_create_job, spawn


def _log(loop_id: str, msg: str) -> None:
    job = get_or_create_job(loop_id)
    # Windows 控制台常为 GBK，替换字符 \ufffd 等会触发 UnicodeEncodeError
    safe = (msg or "").replace("\ufffd", "?").replace("\r", "")
    job.append_log(safe)
    try:
        print(f"[{loop_id}] {safe}", flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(
            f"[{loop_id}] {safe}".encode(enc, errors="replace").decode(enc, errors="replace"),
            flush=True,
        )


def sync_runtime_model(paths: LoopPaths) -> None:
    if not CONFIG_FILE.exists():
        return
    cfg = load_config()
    if paths.runtime_config.exists():
        rc = load_json(paths.runtime_config)
    else:
        rc = dict(cfg)
    rc["model"] = cfg["model"]
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
    eval_prompt = state.get("reports", {}).get("eval_prompt") or state.get("scorer_path") or ""
    eval_args = [
        sys.executable, "eval.py",
        "--input", str(reply),
        "--output", str(eval_out),
        "--config", str(paths.runtime_config),
    ]
    if eval_prompt:
        from loop_lib import resolve_prompt_path
        ep = resolve_prompt_path(eval_prompt)
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


def run_cmd(loop_id: str, args: list[str], check: bool = True) -> int:
    """实时把子进程 stdout/stderr 刷进作业日志，避免长时间静默像卡死。"""
    _log(loop_id, f">> {' '.join(args)}")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        args,
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        _log(loop_id, line.rstrip("\n\r").replace("\ufffd", "?"))
    code = proc.wait()
    if check and code != 0:
        raise RuntimeError(f"命令失败 (exit={code}): {' '.join(args)}")
    return code


def phase_running(state: dict, version: str, ignore_fail: bool) -> dict:
    loop_id = state["loop_id"]
    paths = LoopPaths(loop_id)
    sync_runtime_model(paths)
    gaps = count_batch_gaps(loop_id, version)
    _log(
        loop_id,
        f"===== {version.upper()} 跑批 ===== "
        f"(回复缺 {gaps['pending_reply']} / 打分缺 {gaps['pending_score']})",
    )
    run_args, eval_args = batch_cmds(state, version)
    # 打分默认并发偏低；缺分补跑时抬高一点
    if gaps["pending_score"] > 0 and "--workers" not in eval_args:
        eval_args += ["--workers", "4"]

    if gaps["pending_reply"] > 0 or gaps["total"] == 0:
        run_cmd(loop_id, run_args)
    else:
        _log(loop_id, f"回复已齐全（成功 {gaps['success']}/{gaps['total']}），跳过 run.py")

    if gaps["pending_score"] > 0 or gaps["pending_reply"] > 0 or gaps["total"] == 0:
        run_cmd(loop_id, eval_args)
    else:
        _log(loop_id, "打分已齐全，跳过 eval.py")

    result = mark_batch(loop_id, version, ignore_fail=ignore_fail)
    _log(
        loop_id,
        f"mark-batch: phase={result.get('phase')} ok={result.get('ok')} "
        f"fail={result.get('fail_count', 0)} unscored={result.get('unscored_count', 0)}",
    )
    if result.get("hint"):
        _log(loop_id, f"hint: {result['hint']}")
    return read_state(loop_id)


def phase_deep_diving(state: dict) -> dict:
    loop_id = state["loop_id"]
    _log(loop_id, "===== 深钻诊断 =====")
    run_cmd(
        loop_id,
        [sys.executable, "loop_analyze.py", "--loop-id", loop_id, "--workers", "4"],
        check=False,
    )
    paths = LoopPaths(loop_id)
    if not paths.diagnosis.exists():
        _log(loop_id, "诊断未生成，尝试兜底...")
        run_cmd(
            loop_id,
            [sys.executable, "loop_analyze.py", "--loop-id", loop_id, "--skip-synth"],
            check=False,
        )
    return read_state(loop_id)


def phase_comparing_auto(state: dict) -> dict:
    loop_id = state["loop_id"]
    paths = LoopPaths(loop_id)
    _log(loop_id, "===== v1/v2 对比 =====")
    if not paths.compare.exists():
        run_compare(loop_id)
    else:
        # ensure phase stays COMPARING
        st = read_state(loop_id)
        if st["phase"] != "COMPARING":
            set_phase(st, "COMPARING")
    return read_state(loop_id)


def advance_until_hitl(loop_id: str, ignore_fail: bool = True) -> None:
    """Run automatic phases until a human gate or terminal state."""
    job = get_or_create_job(loop_id)
    job.set("running", "后台推进中…")
    try:
        while True:
            state = read_state(loop_id)
            phase = state["phase"]
            _log(loop_id, f"######## 当前阶段: {phase} ########")

            if phase == "DONE":
                job.set("done", "闭环完成")
                return
            if phase == "FAILED":
                job.set("waiting_hitl", "跑批失败，可忽略失败继续或修复后重跑")
                return
            if phase == "WAITING_ANNOTATION":
                job.set("waiting_hitl", "等待人工校准")
                return
            if phase == "OPTIMIZING":
                job.set("waiting_hitl", "等待 PE 优化与采纳")
                return
            if phase == "COMPARING":
                paths = LoopPaths(loop_id)
                if not paths.compare.exists():
                    phase_comparing_auto(state)
                job.set("waiting_hitl", "等待最终决策")
                return
            if phase == "IDLE":
                set_phase(state, "RUNNING_V1")
                continue
            if phase == "RUNNING_V1":
                state = phase_running(state, "v1", ignore_fail)
                continue
            if phase == "DEEP_DIVING":
                state = phase_deep_diving(state)
                continue
            if phase == "RUNNING_V2":
                state = phase_running(state, "v2", ignore_fail)
                continue
            raise RuntimeError(f"未知阶段: {phase}")
    except Exception as e:
        job.set("error", f"编排失败: {e}", error=str(e))
        _log(loop_id, f"ERROR: {e}")
        raise


def list_agents() -> list[dict[str, str]]:
    """可选智能体：config.json 内置 + 根目录 PE 文件。"""
    items: list[dict[str, str]] = []
    if CONFIG_FILE.exists():
        cfg = load_config()
        agent = cfg.get("agent") or {}
        items.append({
            "id": agent.get("code") or "config",
            "name": agent.get("agent_name") or "config.json 智能体",
            "pe_file": "",
            "source": "config",
        })
    from loop_lib import GENERATOR_PE, SCORER_AGENTFORGE_PE, SCORER_PE
    if GENERATOR_PE.exists():
        rel = str(GENERATOR_PE.relative_to(SCRIPT_DIR)).replace("\\", "/")
        items.append({
            "id": "generator",
            "name": "模拟客户回复",
            "pe_file": rel,
            "source": "agents",
        })
    return items


def list_scorers() -> list[dict[str, str]]:
    from loop_lib import SCORER_AGENTFORGE_PE, SCORER_PE
    candidates = [
        ("default", "默认评分器", SCORER_PE),
        ("agentforge", "AgentForge 评分器", SCORER_AGENTFORGE_PE),
    ]
    out: list[dict[str, str]] = []
    for sid, name, path in candidates:
        p = Path(path)
        if p.exists():
            try:
                rel = str(p.relative_to(SCRIPT_DIR)).replace("\\", "/")
            except ValueError:
                rel = str(p)
            out.append({"id": sid, "name": name, "path": rel})
    return out


def resolve_agent(agent_id: str) -> tuple[str, str, str]:
    """返回 (agent_id, agent_name, pe_file)。"""
    for a in list_agents():
        if a["id"] == agent_id:
            return a["id"], a["name"], a.get("pe_file") or ""
    agents = list_agents()
    if not agents:
        raise FileNotFoundError("无可用智能体（请配置 config.json）")
    a = agents[0]
    return a["id"], a["name"], a.get("pe_file") or ""


def resolve_scorer(scorer_id: str) -> tuple[str, str]:
    """返回 (scorer_id, path)。"""
    for s in list_scorers():
        if s["id"] == scorer_id:
            return s["id"], s["path"]
    scorers = list_scorers()
    if not scorers:
        raise FileNotFoundError("无可用评分器（缺少 agents/scorer/system.md）")
    s = scorers[0]
    return s["id"], s["path"]


def start_and_advance(
    dataset: str,
    scenes: str = "",
    items: str = "",
    pe_file: str = "",
    ignore_fail: bool = True,
    *,
    agent_id: str = "",
    scorer_id: str = "",
) -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError("缺少 config.json，请先从 config.example.json 复制并填写密钥")
    aid, aname, pe = resolve_agent(agent_id) if agent_id else resolve_agent("")
    if pe_file:
        pe = pe_file
    sid, spath = resolve_scorer(scorer_id) if scorer_id else resolve_scorer("")
    result = start_loop(
        dataset=dataset,
        scenes=scenes,
        items=items,
        pe_file=pe or "",
        agent_id=aid,
        agent_name=aname,
        scorer=spath,
        scorer_id=sid,
    )
    loop_id = result["loop_id"]
    get_or_create_job(loop_id).set("running", "已创建，开始 v1 跑批…")
    if not spawn(loop_id, advance_until_hitl, loop_id, ignore_fail):
        raise RuntimeError(f"loop {loop_id} 已有后台任务在运行")
    return result


def resume_advance(loop_id: str, ignore_fail: bool = True, force_phase: str = "") -> dict:
    state = read_state(loop_id)
    if state["phase"] == "FAILED" and ignore_fail and not force_phase:
        # 按 v1 失败忽略并推进到诊断
        version = "v1"
        if state.get("stats", {}).get("v2"):
            version = "v2"
        mark_batch(loop_id, version, ignore_fail=True)
    elif force_phase:
        set_phase(state, force_phase)
    if not spawn(loop_id, advance_until_hitl, loop_id, ignore_fail):
        raise RuntimeError(f"loop {loop_id} 已有后台任务在运行")
    get_or_create_job(loop_id).set("running", "续跑中…")
    return {"ok": True, "loop_id": loop_id}


def _row_needs_retry(row: dict) -> bool:
    status = (row.get("执行结果") or "").strip()
    if status != "成功":
        return True
    if not (row.get("主题相关性_分") or "").strip():
        return True
    return False


def count_batch_gaps(loop_id: str, version: str) -> dict[str, Any]:
    paths = LoopPaths(loop_id)
    reply = paths.v1_reply if version == "v1" else paths.v2_reply
    eval_path = paths.v1_eval if version == "v1" else paths.v2_eval
    path = eval_path if eval_path.exists() else reply
    if not path.exists():
        return {
            "version": version,
            "total": 0,
            "success": 0,
            "pending_reply": 0,
            "pending_score": 0,
            "pending": 0,
        }
    _, rows = load_xlsx_rows(path)
    success = 0
    pending_reply = 0
    pending_score = 0
    for row in rows:
        status = (row.get("执行结果") or "").strip()
        scored = bool((row.get("主题相关性_分") or "").strip())
        if status == "成功":
            success += 1
            if not scored:
                pending_score += 1
        else:
            pending_reply += 1
    return {
        "version": version,
        "total": len(rows),
        "success": success,
        "pending_reply": pending_reply,
        "pending_score": pending_score,
        "pending": pending_reply + pending_score,
    }


def resolve_retry_version(state: dict, version: str = "") -> str:
    if version in ("v1", "v2"):
        return version
    phase = state.get("phase") or ""
    if phase in ("RUNNING_V2", "COMPARING"):
        return "v2"
    if phase == "DONE" and (state.get("pe") or {}).get("active") == "v2":
        return "v2"
    paths = LoopPaths(state["loop_id"])
    if phase in ("FAILED",) and paths.v2_reply.exists() and not paths.v1_eval.exists():
        return "v2"
    return "v1"


def retry_incomplete_batch(
    loop_id: str,
    version: str = "",
    ignore_fail: bool = True,
) -> dict[str, Any]:
    """同步最新 API Key，仅重跑未成功/未打分条目，然后继续推进。"""
    state = read_state(loop_id)
    paths = LoopPaths(loop_id)
    sync_runtime_model(paths)
    ver = resolve_retry_version(state, version)
    gaps = count_batch_gaps(loop_id, ver)
    if gaps["pending"] <= 0:
        return {
            "ok": True,
            "loop_id": loop_id,
            "version": ver,
            "pending": 0,
            "message": "没有未成功条目需要重跑",
            "gaps": gaps,
        }
    target_phase = "RUNNING_V1" if ver == "v1" else "RUNNING_V2"
    set_phase(state, target_phase)
    if not spawn(loop_id, advance_until_hitl, loop_id, ignore_fail):
        raise RuntimeError(f"loop {loop_id} 已有后台任务在运行")
    get_or_create_job(loop_id).set(
        "running",
        f"重跑 {ver.upper()} 未成功条目（回复缺 {gaps['pending_reply']} / 打分缺 {gaps['pending_score']}）…",
    )
    return {
        "ok": True,
        "loop_id": loop_id,
        "version": ver,
        "pending": gaps["pending"],
        "gaps": gaps,
        "phase": target_phase,
    }


def do_annotate(
    loop_id: str,
    business_summary: str,
    adopted_indices: list[int],
    adopt_all: bool,
) -> dict:
    paths = LoopPaths(loop_id)
    if not paths.diagnosis.exists():
        raise FileNotFoundError("缺少诊断报告")
    diag = load_json(paths.diagnosis)
    oa = diag.get("overall_assessment") or {}
    opt = oa.get("optimization_priority") or []
    if not isinstance(opt, list):
        opt = []

    if adopt_all or not adopted_indices:
        adopted = opt
        rejected: list = []
    else:
        idxs = set(adopted_indices)
        adopted = [opt[i - 1] for i in sorted(idxs) if 1 <= i <= len(opt)]
        rejected = [opt[i - 1] for i, _ in enumerate(opt, 1) if i not in idxs]

    summary = business_summary or str(oa.get("quality_diagnosis") or "")
    payload = {
        "business_summary": summary,
        "adopted_directions": adopted,
        "rejected_directions": rejected,
        "confirmed_via": "web",
    }
    result = submit_annotation(loop_id, payload, confirmed=True)
    get_or_create_job(loop_id).set("waiting_hitl", "校准已提交，请优化 PE")
    return result


def do_optimize(loop_id: str, mode: str) -> dict:
    paths = LoopPaths(loop_id)
    job = get_or_create_job(loop_id)
    if mode == "auto":
        job.set("running", "LLM 生成 PE 草稿…")
        try:
            text = auto_optimize_draft(paths, log=lambda m: _log(loop_id, m))
        finally:
            job.set("waiting_hitl", "草稿已生成，可编辑或采纳")
        return {"ok": True, "mode": "auto", "chars": len(text), "preview": text[:2000]}
    draft = paths.pe_draft.read_text(encoding="utf-8") if paths.pe_draft.exists() else ""
    job.set("waiting_hitl", "沿用当前草稿")
    return {"ok": True, "mode": "keep", "chars": len(draft), "preview": draft[:2000]}


def do_adopt_and_advance(loop_id: str, ignore_fail: bool = True) -> dict:
    result = adopt_draft(loop_id)
    get_or_create_job(loop_id).set("running", "已采纳 v2，开始跑批…")
    if not spawn(loop_id, advance_until_hitl, loop_id, ignore_fail):
        raise RuntimeError(f"loop {loop_id} 已有后台任务在运行")
    return result


def do_decide(
    loop_id: str,
    decision: str,
    note: str = "",
    write_config: bool = True,
) -> dict:
    result = apply_decision(
        loop_id, decision, note, confirmed=True, write_config=write_config
    )
    get_or_create_job(loop_id).set("done", f"决策完成: {decision}")
    return result


def list_datasets() -> list[dict[str, str]]:
    names: list[dict[str, str]] = []
    for p in sorted(SCRIPT_DIR.iterdir()):
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if p.suffix.lower() in (".xlsx", ".xlsm", ".csv") or "测评集" in p.name:
            if p.suffix.lower() in (".xlsx", ".xlsm", ".csv") or p.name.endswith(".csv"):
                names.append({"name": p.name, "path": p.name})
    # dedupe
    seen = set()
    out = []
    for item in names:
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        out.append(item)
    return out


def read_eval_preview(loop_id: str, version: str, limit: int | None = None) -> dict:
    paths = LoopPaths(loop_id)
    path = paths.v1_eval if version == "v1" else paths.v2_eval
    if not path.exists():
        return {"columns": [], "rows": [], "exists": False}
    headers, rows = load_xlsx_rows(path)
    # 身份列 + 生产回复 + 综合分 + 全部维度打分（*_分）
    lead = [c for c in ("instance_id", "title", "customer_name", "生产回复", "执行结果", "综合得分") if c in headers]
    score_cols = [c for c in headers if c.endswith("_分")]
    # 去重保序：lead 优先，其余 *_分 按表头顺序补齐
    seen: set[str] = set()
    cols: list[str] = []
    for c in lead + score_cols:
        if c not in seen:
            seen.add(c)
            cols.append(c)
    sliced = rows if limit is None else rows[:limit]
    preview = [{c: row.get(c, "") for c in cols} for row in sliced]
    return {"columns": cols, "rows": preview, "exists": True, "total": len(rows)}


def load_report(loop_id: str, name: str) -> Any:
    paths = LoopPaths(loop_id)
    mapping = {
        "diagnosis": paths.diagnosis,
        "annotation": paths.annotation,
        "compare": paths.compare,
    }
    path = mapping.get(name)
    if path is None:
        raise ValueError(f"未知报告: {name}")
    if not path.exists():
        raise FileNotFoundError(f"报告不存在: {path}")
    return load_json(path)


def get_pe(loop_id: str, which: str) -> str:
    paths = LoopPaths(loop_id)
    file_map = {
        "v1": paths.pe_v1,
        "draft": paths.pe_draft,
        "v2": paths.pe_v2,
    }
    path = file_map.get(which)
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def save_draft(loop_id: str, content: str) -> dict:
    paths = LoopPaths(loop_id)
    paths.pe_draft.write_text(content, encoding="utf-8")
    return {"ok": True, "chars": len(content)}


def build_loop_detail(loop_id: str) -> dict:
    from loop_ctl import get_status

    from .jobs import get_job

    status = get_status(loop_id)
    job = get_job(loop_id)
    job_snap = job.snapshot() if job else {
        "job_state": "idle",
        "message": "",
        "error": None,
        "log_tail": [],
    }
    paths = LoopPaths(loop_id)
    diagnosis_summary = None
    if paths.diagnosis.exists():
        diag = load_json(paths.diagnosis)
        oa = diag.get("overall_assessment") or {}
        opt = oa.get("optimization_priority") or []
        diagnosis_summary = {
            "avg_score": diag.get("avg_score"),
            "sample_count": diag.get("sample_count"),
            "quality_diagnosis": oa.get("quality_diagnosis"),
            "key_bottleneck": oa.get("key_bottleneck"),
            "systemic_issue": oa.get("systemic_issue"),
            "optimization_priority": opt,
        }
    compare = load_json(paths.compare) if paths.compare.exists() else None
    phase = status.get("phase") or ""
    gap_v = resolve_retry_version(status, "")
    # 两版都给前端，按钮按当前版本 pending 显示
    gaps = {
        "v1": count_batch_gaps(loop_id, "v1"),
        "v2": count_batch_gaps(loop_id, "v2"),
        "active": gap_v,
    }
    return {
        **status,
        "job": job_snap,
        "diagnosis_summary": diagnosis_summary,
        "compare": compare,
        "batch_gaps": gaps,
        "phase_label_hint": phase,
    }
