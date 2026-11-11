"""自进化闭环共享库：loop 路径、state.json、提示词路径、LLM/JSON 工具。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

SCRIPT_DIR = Path(__file__).parent
LOOPS_DIR = SCRIPT_DIR / "loops"
CONFIG_FILE = SCRIPT_DIR / "config.json"
AGENTS_DIR = SCRIPT_DIR / "agents"
SKILLS_DIR = SCRIPT_DIR / "skills"

# 角色提示词（agents/<role>/）
GENERATOR_PE = AGENTS_DIR / "generator" / "system.md"
SCORER_PE = AGENTS_DIR / "scorer" / "system.md"
SCORER_AGENTFORGE_PE = AGENTS_DIR / "scorer" / "agentforge.md"
ANALYZER_DIM_PE = AGENTS_DIR / "analyzer" / "dimension.md"
ANALYZER_SYNTH_PE = AGENTS_DIR / "analyzer" / "synthesizer.md"
OPTIMIZER_PE = AGENTS_DIR / "optimizer" / "system.md"
ORCHESTRATOR_PE = AGENTS_DIR / "orchestrator" / "system.md"
DATASET_BUILD_DOC = SKILLS_DIR / "dataset-build" / "build.md"

PHASES = (
    "IDLE",
    "RUNNING_V1",
    "FAILED",
    "DEEP_DIVING",
    "WAITING_ANNOTATION",
    "OPTIMIZING",
    "RUNNING_V2",
    "COMPARING",
    "DONE",
)

DIM_NAMES = [
    "主题相关性",
    "话题边界守护",
    "信息递进合理性",
    "人设信息忠实度",
    "回复独立语义值",
    "关键词脱敏度",
    "HWP跟随克制度",
    "情绪响应匹配度",
    "关注点克制度",
    "专业术语回避",
    "流程推进被动性",
    "错误信息纠正",
    "语气人设匹配",
    "语句流畅自然度",
    "口语化程度",
    "角色立场独立性",
    "回复长度合理性",
]

HARD_DIMS = {
    "主题相关性",
    "话题边界守护",
    "关键词脱敏度",
    "HWP跟随克制度",
    "关注点克制度",
    "场景背景关联度",
    "错误信息纠正",
    "口语化程度",
}


class LoopPaths:
    def __init__(self, loop_id: str):
        self.loop_id = loop_id
        self.root = LOOPS_DIR / loop_id
        self.state_file = self.root / "state.json"
        self.memory_file = self.root / "MEMORY.md"
        self.pe_dir = self.root / "pe"
        self.pe_v1 = self.pe_dir / "v1.md"
        self.pe_draft = self.pe_dir / "draft.md"
        self.pe_v2 = self.pe_dir / "v2.md"
        self.reports_dir = self.root / "reports"
        self.diagnosis = self.reports_dir / "diagnosis.json"
        self.annotation = self.reports_dir / "annotation.json"
        self.compare = self.reports_dir / "compare.json"
        self.runs_dir = self.root / "runs"
        self.v1_reply = self.runs_dir / "v1_reply.xlsx"
        self.v1_eval = self.runs_dir / "v1_eval.xlsx"
        self.v2_reply = self.runs_dir / "v2_reply.xlsx"
        self.v2_eval = self.runs_dir / "v2_eval.xlsx"
        self.runtime_config = self.root / "runtime_config.json"

    def ensure_dirs(self) -> None:
        for d in (self.root, self.pe_dir, self.reports_dir, self.runs_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_config(path: Path | None = None) -> dict:
    cfg_path = path or CONFIG_FILE
    with open(cfg_path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_loop_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_state(
    loop_id: str,
    dataset: str,
    scenes: str = "",
    items: str = "",
    *,
    agent_id: str = "",
    agent_name: str = "",
    scorer_id: str = "",
    scorer_path: str = "",
) -> dict:
    paths = LoopPaths(loop_id)

    def rel(p: Path) -> str:
        return p.relative_to(SCRIPT_DIR).as_posix()

    return {
        "loop_id": loop_id,
        "phase": "IDLE",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "dataset": dataset.replace("\\", "/"),
        "scenes": scenes,
        "items": items,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "scorer_id": scorer_id,
        "scorer_path": scorer_path.replace("\\", "/") if scorer_path else "",
        "pe": {
            "v1": rel(paths.pe_v1),
            "draft": rel(paths.pe_draft),
            "v2": rel(paths.pe_v2),
            "active": "v1",
        },
        "runs": {
            "v1_reply": rel(paths.v1_reply),
            "v1_eval": rel(paths.v1_eval),
            "v2_reply": rel(paths.v2_reply),
            "v2_eval": rel(paths.v2_eval),
        },
        "reports": {
            "diagnosis": rel(paths.diagnosis),
            "annotation": rel(paths.annotation),
            "compare": rel(paths.compare),
        },
        "stats": {
            "v1": {},
            "v2": {},
        },
        "decision": None,
        "notes": "",
    }


def read_state(loop_id: str) -> dict:
    paths = LoopPaths(loop_id)
    if not paths.state_file.exists():
        raise FileNotFoundError(f"loop 不存在: {paths.state_file}")
    return load_json(paths.state_file)


def write_state(state: dict) -> None:
    loop_id = state["loop_id"]
    paths = LoopPaths(loop_id)
    paths.ensure_dirs()
    state["updated_at"] = now_iso()
    save_json(paths.state_file, state)


def set_phase(state: dict, phase: str) -> dict:
    if phase not in PHASES:
        raise ValueError(f"非法 phase: {phase}，允许: {', '.join(PHASES)}")
    state["phase"] = phase
    write_state(state)
    return state


def resolve_loop_id(loop_id: str | None) -> str:
    if loop_id:
        return loop_id
    if not LOOPS_DIR.exists():
        raise SystemExit("尚无任何 loop，请先 loop_ctl.py start")
    candidates = sorted(
        [p for p in LOOPS_DIR.iterdir() if p.is_dir() and (p / "state.json").exists()],
        key=lambda p: p.name,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("尚无任何 loop，请先 loop_ctl.py start")
    return candidates[0].name


def make_openai_client(model_cfg: dict) -> OpenAI:
    base_url = (
        model_cfg["baseUrl"].rstrip("/")
        + model_cfg["endpoint"].replace("/chat/completions", "").rstrip("/")
    )
    return OpenAI(base_url=base_url, api_key=model_cfg["apiKey"], timeout=180)


def call_llm(
    client: OpenAI,
    model: str,
    messages: list[dict],
    model_cfg: dict,
    retries: int = 2,
) -> dict:
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            params: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": model_cfg.get("temperature", 0.3),
                "top_p": model_cfg.get("topP", 0.9),
            }
            if model_cfg.get("maxTokens"):
                params["max_tokens"] = model_cfg["maxTokens"]
            resp = client.chat.completions.create(**params)
            text = resp.choices[0].message.content or ""
            return {
                "success": True,
                "reply": text,
                "cost_ms": (time.time() - t0) * 1000,
            }
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(2)
    return {"success": False, "error": last_err, "cost_ms": 0.0}


def extract_json(text: str) -> dict:
    if not text:
        raise ValueError("空响应")
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def render_template(template: str, variables: dict) -> str:
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", value if value is not None else "")
        result = result.replace("${" + key + "}", value if value is not None else "")
    return result


def write_runtime_config(paths: LoopPaths, pe_text: str, base_config: dict | None = None) -> Path:
    cfg = dict(base_config or load_config())
    agent = dict(cfg.get("agent") or {})
    agent["system_prompt"] = pe_text
    cfg["agent"] = agent
    save_json(paths.runtime_config, cfg)
    return paths.runtime_config


def init_memory(paths: LoopPaths, dataset: str) -> None:
    if paths.memory_file.exists():
        return
    paths.memory_file.write_text(
        f"""# Loop Memory

- loop_id: `{paths.loop_id}`
- dataset: `{dataset}`
- goals: 提升面谈客户回复 Agent 综合得分，降低硬违规维度 C/D 率
- findings:
- next_steps: 启动 RUNNING_V1 跑批
""",
        encoding="utf-8",
    )


def load_xlsx_rows(path: Path) -> tuple[list[str], list[dict]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    ws = wb.active
    headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    rows: list[dict] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        item: dict[str, str] = {}
        for i, name in enumerate(headers):
            if name is None:
                continue
            v = row[i] if i < len(row) else None
            item[str(name)] = str(v).strip() if v is not None else ""
        rows.append(item)
    wb.close()
    return [str(h) for h in headers if h is not None], rows


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def require_confirmation(
    *,
    confirmed: bool = False,
    interactive: bool = False,
    action: str = "该操作",
) -> None:
    """人工门控：无确认且非交互时拒绝。"""
    if confirmed or interactive:
        return
    raise PermissionError(
        f"拒绝执行：{action} 需要人工确认。"
        "请使用 --confirmed / --interactive，或通过 Web UI 确认。"
    )


def resolve_prompt_path(path_or_name: str | Path) -> Path:
    """解析提示词路径；兼容旧根目录中文文件名与已废弃的 skillbox/ 前缀。"""
    aliases = {
        "测评提示词.md": SCORER_PE,
        "面课回复生成提示词.md": GENERATOR_PE,
        "优化器.md": OPTIMIZER_PE,
        "编排器.md": ORCHESTRATOR_PE,
        "维度分析器.md": ANALYZER_DIM_PE,
        "合成器.md": ANALYZER_SYNTH_PE,
        "测评集构建Skill.md": DATASET_BUILD_DOC,
        "agentforge平台测评集/测评提示词.md": (
            SCORER_AGENTFORGE_PE
            if SCORER_AGENTFORGE_PE.exists()
            else SCRIPT_DIR / "agentforge平台测评集" / "测评提示词.md"
        ),
    }
    raw = str(path_or_name).replace("\\", "/").strip()
    if not raw:
        return SCORER_PE
    if raw in aliases:
        return Path(aliases[raw])
    p = Path(path_or_name)
    if p.is_absolute():
        return p
    candidate = SCRIPT_DIR / p
    if candidate.exists():
        return candidate
    name = Path(raw).name
    if name in aliases:
        return Path(aliases[name])
    if raw.startswith("skillbox/"):
        rest = raw[len("skillbox/") :]
        if rest.startswith("dataset"):
            alt = SKILLS_DIR / rest.replace("dataset/", "dataset-build/", 1)
        else:
            alt = AGENTS_DIR / rest
        if alt.exists():
            return alt
    if raw.startswith("harness/"):
        return resolve_prompt_path(raw.split("/", 1)[-1])
    return candidate


def summarize_eval_rows(rows: list[dict]) -> dict:
    scores: list[float] = []
    ok = 0
    fail = 0
    dim_dist: dict[str, dict[str, int]] = {
        d: {"A": 0, "B": 0, "C": 0, "D": 0, "N/A": 0} for d in DIM_NAMES
    }
    for row in rows:
        status = row.get("执行结果", "")
        if status == "成功" or row.get("生产回复"):
            if row.get("主题相关性_分"):
                ok += 1
            elif status and status != "成功":
                fail += 1
        total = row.get("综合得分", "")
        if total not in ("", None):
            scores.append(safe_float(total))
        for dim in DIM_NAMES:
            grade = (row.get(f"{dim}_分") or "N/A").strip().upper()
            if grade not in dim_dist[dim]:
                grade = "N/A"
            dim_dist[dim][grade] += 1
    avg = round(sum(scores) / len(scores), 2) if scores else None
    return {
        "scored_count": len(scores),
        "avg_score": avg,
        "ok": ok,
        "fail": fail,
        "dimension_grade_distribution": dim_dist,
    }
