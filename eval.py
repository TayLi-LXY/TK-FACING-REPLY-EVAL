import json
import os
import re
import time
import shutil
import signal
import argparse
import atexit
import threading
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import openpyxl
from openpyxl import load_workbook
from openai import OpenAI


SCRIPT_DIR = Path(__file__).parent
EVAL_FILE = SCRIPT_DIR / "回复输出目录" / "回复输出.xlsx"
OUTPUT_FILE = SCRIPT_DIR / "测评输出目录" / "测评输出.xlsx"
OUTPUT_TMP_FILE = SCRIPT_DIR / "测评输出目录" / "测评输出_tmp.xlsx"
LOG_DIR = SCRIPT_DIR / "日志"
try:
    from loop_lib import SCORER_PE
    EVAL_PROMPT_FILE = SCORER_PE
except Exception:
    EVAL_PROMPT_FILE = SCRIPT_DIR / "agents" / "scorer" / "system.md"
CONFIG_PATH = SCRIPT_DIR / "config.json"

_stop_event = threading.Event()
_wb_ref = [None]

COL = {
    "instance_id": "instance_id",
    "title": "title",
    "target": "target",
    "customer_name": "customer_name",
    "customer_gender": "customer_gender",
    "customer_age": "customer_age",
    "customer_personality": "customer_personality",
    "customer_personality_detail": "customer_personality_detail",
    "customer_tags": "customer_tags",
    "customer_extras": "customer_extras",
    "background": "background",
    "history": "history_messages",
    "current_msg": "current_hwp_message",
    "reply": "生产回复",
    "status": "执行结果",
    "violation": "违规类型",
}

DIM_MAP = {
    "主题相关性": "主题相关性",
    "话题边界守护": "话题边界守护",
    "信息递进合理性": "信息递进合理性",
    "人设信息忠实度": "人设信息忠实度",
    "回复独立语义值": "回复独立语义值",
    "关键词脱敏度": "关键词脱敏度",
    "HWP跟随克制度": "HWP跟随克制度",
    "情绪响应匹配度": "情绪响应匹配度",
    "关注点克制度": "关注点克制度",
    "专业术语回避": "专业术语回避",
    "流程推进被动性": "流程推进被动性",
    "错误信息纠正": "错误信息纠正",
    "语气人设匹配": "语气人设匹配",
    "语句流畅自然度": "语句流畅自然度",
    "口语化程度": "口语化程度",
    "角色立场独立性": "角色立场独立性",
    "回复长度合理性": "回复长度合理性",
}

_progress_lock = threading.Lock()
_log_lock = threading.Lock()
_llm_log_file = None
_header_to_idx: dict[str, int] = {}
SAVE_INTERVAL = 10


def parse_scenes_arg(scenes_str: str) -> list[str]:
    parts = [p.strip() for p in scenes_str.split(",") if p.strip()]
    result = []
    for part in parts:
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a.strip()), int(b.strip())
            result.extend(str(i) for i in range(start, end + 1))
        else:
            result.append(part)
    return result


def load_config(path: Path | None = None):
    cfg = path or CONFIG_PATH
    with open(cfg, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def load_eval_prompt_template() -> str:
    text = EVAL_PROMPT_FILE.read_text(encoding="utf-8")
    marker = "# 待评测数据"
    if marker in text:
        text = text[: text.index(marker)].rstrip()
    return text


def init_log(run_ts: str):
    global _llm_log_file
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _llm_log_file = LOG_DIR / f"eval_{run_ts}.jsonl"
    print(f"  日志文件: {_llm_log_file}")


def log_llm_call(scene_id: str, scene_instance_id: str, worker_id: str,
                 messages: list[dict], response_text: str, error: str,
                 cost_ms: float):
    if _llm_log_file is None:
        return
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "instance_id": scene_id,
        "row_key": scene_instance_id,
        "worker": worker_id,
        "cost_ms": round(cost_ms, 1),
        "request": messages,
        "response": response_text if not error else None,
        "error": error if error else None,
    }
    with _log_lock:
        with open(_llm_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def call_llm(client: OpenAI, model: str, messages: list[dict], model_cfg: dict,
             retries: int, scene_id: str, scene_instance_id: str, worker_id: str) -> dict:
    last_err = None
    cost = 0.0
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            params = {
                "model": model,
                "messages": messages,
                "temperature": model_cfg.get("temperature", 0.7),
                "top_p": model_cfg.get("topP", 0.9),
            }
            if model_cfg.get("maxTokens"):
                params["max_tokens"] = model_cfg["maxTokens"]
            if model_cfg.get("presencePenalty"):
                params["presence_penalty"] = model_cfg["presencePenalty"]
            if model_cfg.get("frequencyPenalty"):
                params["frequency_penalty"] = model_cfg["frequencyPenalty"]
            resp = client.chat.completions.create(**params)
            cost = (time.time() - t0) * 1000
            text = resp.choices[0].message.content or ""
            log_llm_call(scene_id, scene_instance_id, worker_id, messages, text, "", cost)
            return {"success": True, "reply": text, "cost_ms": cost}
        except Exception as e:
            last_err = str(e)
            cost = (time.time() - t0) * 1000
            log_llm_call(scene_id, scene_instance_id, worker_id, messages, "", last_err, cost)
            if attempt < retries:
                time.sleep(2)
    return {"success": False, "error": last_err, "cost_ms": cost}


def init_output() -> openpyxl.Workbook:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not OUTPUT_FILE.exists():
        shutil.copy2(EVAL_FILE, OUTPUT_FILE)
        print(f"  评测文件已复制到: {OUTPUT_FILE}")
    else:
        print(f"  复用已有输出文件: {OUTPUT_FILE}")
    wb = load_workbook(OUTPUT_FILE)
    ws = wb.active
    global _header_to_idx
    _header_to_idx = {cell.value: i for i, cell in enumerate(next(ws.iter_rows(min_row=1, max_row=1)))}
    return wb


def load_rows(ws) -> list[dict]:
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        row_dict = {}
        for col_name, col_idx in _header_to_idx.items():
            if col_name is None:
                continue
            v = row[col_idx] if col_idx < len(row) else None
            row_dict[col_name] = str(v or "").strip() if v is not None else ""
        rows.append(row_dict)
    return rows


def is_done(status: str) -> bool:
    """回复执行结果：仅「成功」可进入打分。"""
    return (status or "").strip() == "成功"


def scores_done(row: dict) -> bool:
    return bool((row.get("主题相关性_分") or "").strip())


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
            return json.loads(text[start: end + 1])
        raise


def flatten_scores(eval_obj: dict) -> dict:
    scores: dict[str, tuple[str, str]] = {}
    violations = []
    for group in ("维度一_业务合规性", "维度二_拟人自然度"):
        block = eval_obj.get(group) or {}
        if not isinstance(block, dict):
            continue
        for dim_name, item in block.items():
            col = DIM_MAP.get(dim_name)
            if not col:
                continue
            if isinstance(item, dict):
                grade = str(item.get("档位", "")).strip() or "N/A"
                reason = str(item.get("理由", "")).strip()
            else:
                grade, reason = str(item).strip(), ""
            scores[col] = (grade, reason)
            if grade.upper() in ("C", "D"):
                violations.append(f"{col}:{grade}")
    total = eval_obj.get("综合得分", "")
    note = eval_obj.get("得分说明", "")
    if total is None:
        total = ""
    if note is None:
        note = ""
    return {
        "scores": scores,
        "violations": "；".join(violations),
        "total_score": total,
        "score_note": str(note).strip(),
    }


def build_eval_user_payload(row: dict, reply: str) -> str:
    history = row.get(COL["history"], "") or "[]"
    return f"""# 待评测数据

## 场景信息

- instance_id：{row.get(COL["instance_id"], "")}
- title：{row.get(COL["title"], "")}
- target：{row.get(COL["target"], "")}
- background：{row.get(COL["background"], "")}

## 客户信息

- customer_name：{row.get(COL["customer_name"], "")}
- customer_gender：{row.get(COL["customer_gender"], "")}
- customer_age：{row.get(COL["customer_age"], "")}
- customer_personality：{row.get(COL["customer_personality"], "")}
- customer_personality_detail：{row.get(COL["customer_personality_detail"], "")}
- customer_tags：{row.get(COL["customer_tags"], "")}
- customer_extras：{row.get(COL["customer_extras"], "")}

## 对话内容

- history_messages：

{history}

- current_hwp_message：

{row.get(COL["current_msg"], "")}

- 生产回复（评测对象）：

{reply}

请根据以上信息，按照评测维度逐一打分，严格按 JSON 格式输出。"""


def evaluate_row(row: dict, client: OpenAI, config: dict, eval_sys: str,
                 retries: int, scene_id: str, worker_id: str) -> dict:
    model_cfg = dict(config["model"])
    model_cfg["temperature"] = min(float(model_cfg.get("temperature", 0.7)), 0.3)
    reply = row.get(COL["reply"], "").strip()
    row_key = f"{scene_id}:{reply[:40]}"
    if not reply:
        return {"eval": None, "status": "无生产回复，无法评测", "cost_ms": 0}
    msgs = [
        {"role": "system", "content": eval_sys},
        {"role": "user", "content": build_eval_user_payload(row, reply)},
    ]
    result = call_llm(client, model_cfg["modelName"], msgs, model_cfg,
                      retries, scene_id, row_key, worker_id)
    cost = result.get("cost_ms", 0)
    if not result["success"]:
        return {"eval": None, "status": f"评测失败: {result['error']}", "cost_ms": cost}
    try:
        obj = extract_json(result["reply"])
        payload = flatten_scores(obj)
        return {"eval": payload, "status": "成功", "cost_ms": cost}
    except Exception as e:
        return {"eval": None, "status": f"评测解析失败: {e}", "cost_ms": cost}


def write_eval_result(ws, row_idx: int, eval_result: dict | None):
    if not eval_result:
        return
    scores = eval_result.get("scores") or {}
    for col_prefix, (grade, reason) in scores.items():
        score_col = f"{col_prefix}_分"
        reason_col = f"{col_prefix}_理由"
        if score_col in _header_to_idx:
            ws.cell(row=row_idx, column=_header_to_idx[score_col] + 1).value = grade
        if reason_col in _header_to_idx:
            ws.cell(row=row_idx, column=_header_to_idx[reason_col] + 1).value = reason
    if COL["violation"] in _header_to_idx:
        ws.cell(row=row_idx, column=_header_to_idx[COL["violation"]] + 1).value = (
            eval_result.get("violations") or ""
        )
    if "综合得分" in _header_to_idx and eval_result.get("total_score") not in (None, ""):
        ws.cell(row=row_idx, column=_header_to_idx["综合得分"] + 1).value = (
            eval_result.get("total_score")
        )
    if "得分说明" in _header_to_idx and eval_result.get("score_note"):
        ws.cell(row=row_idx, column=_header_to_idx["得分说明"] + 1).value = (
            eval_result.get("score_note")
        )


def _print_progress(row_idx: int, scene_id: str, done: int, total: int,
                    ok: int, fail: int, avg_cost: float, status: str):
    bar_len = 20
    filled = bar_len * done // max(total, 1)
    bar = "#" * filled + "-" * (bar_len - filled)
    eta_sec = int((total - done) * avg_cost / 1000) if avg_cost > 0 else 0
    m, s = divmod(eta_sec, 60)
    print(f"  [行{row_idx}] 实例{scene_id}  [{bar}] {done:03d}/{total:03d} "
          f"ok={ok} fail={fail} 均:{avg_cost/1000:.1f}s ETA {m:02d}:{s:02d}  {status}", flush=True)


def _do_save(wb: openpyxl.Workbook, label: str = ""):
    print(f"  [保存{label}] 写入 {OUTPUT_FILE.name} ...", flush=True)
    t0 = time.time()
    try:
        wb.save(OUTPUT_TMP_FILE)
        os.replace(OUTPUT_TMP_FILE, OUTPUT_FILE)
        print(f"  [保存{label}] 完成 {(time.time()-t0)*1000:.0f}ms", flush=True)
    except PermissionError:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        alt = OUTPUT_FILE.parent / f"{OUTPUT_FILE.stem}_{ts}{OUTPUT_FILE.suffix}"
        try:
            os.replace(OUTPUT_TMP_FILE, alt)
        except Exception:
            wb.save(alt)
        print(f"  [保存{label}] 原文件被占用，已另存为: {alt.name}", flush=True)
    except Exception as e:
        print(f"  [保存{label}] 失败: {e}", flush=True)


def run_batch(items: list[tuple[str, int, dict]], client: OpenAI, config: dict,
              eval_sys: str, retries: int, force: bool,
              workers: int, wb: openpyxl.Workbook, ws) -> dict:
    wb_lock = threading.Lock()
    overall = {"done": 0, "total": len(items), "ok": 0, "fail": 0}
    cost_samples: list[float] = []
    pending_saves: list[int] = []

    def _process_and_write(item):
        if _stop_event.is_set():
            return {"eval": None, "status": "已停止", "cost_ms": 0, "skip": True, "stopped": True}
        scene_id, row_idx, row = item
        worker_id = f"w-{threading.current_thread().name}"
        if scores_done(row) and not force:
            return {"eval": None, "status": "跳过", "cost_ms": 0, "skip": True}
        result = evaluate_row(row, client, config, eval_sys, retries, scene_id, worker_id)
        result["skip"] = False
        result["stopped"] = False
        with wb_lock:
            write_eval_result(ws, row_idx, result["eval"])
            pending_saves.append(row_idx)
            if len(pending_saves) >= SAVE_INTERVAL:
                _do_save(wb)
                pending_saves.clear()
        with _progress_lock:
            overall["done"] += 1
            ok = result["status"] == "成功"
            if ok:
                overall["ok"] += 1
            else:
                overall["fail"] += 1
            cost_samples.append(result["cost_ms"])
            avg = sum(cost_samples) / len(cost_samples)
            status_icon = "ok" if ok else f"fail:{result['status'][:40]}"
            _print_progress(row_idx, scene_id, overall["done"], overall["total"],
                            overall["ok"], overall["fail"], avg, status_icon)
        return result

    if workers <= 1:
        for item in items:
            _process_and_write(item)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_process_and_write, item) for item in items]
            for f in as_completed(futures):
                f.result()

    if pending_saves:
        _do_save(wb)
    skipped_count = len(items) - overall["done"]
    print(f"\n======== 批次完成 ========")
    print(f"ok={overall['ok']}  fail={overall['fail']}  skip={skipped_count}", flush=True)
    return overall


def _signal_handler(sig, frame):
    _stop_event.set()
    print("\n  [收到 Ctrl+C] 正在等待当前任务完成后退出...", flush=True)
    print("  强制退出请再次按 Ctrl+C", flush=True)


def sync_replies_from_source(ws):
    """把回复输出中的生产回复/执行结果同步到测评输出，避免旧副本漏掉新生成。"""
    src = load_workbook(EVAL_FILE)
    src_ws = src.active
    src_h = {cell.value: i for i, cell in enumerate(next(src_ws.iter_rows(min_row=1, max_row=1)))}
    for col_name in (COL["reply"], COL["status"]):
        if col_name not in src_h or col_name not in _header_to_idx:
            continue
        src_col = src_h[col_name] + 1
        dst_col = _header_to_idx[col_name] + 1
        for r in range(2, src_ws.max_row + 1):
            ws.cell(row=r, column=dst_col).value = src_ws.cell(row=r, column=src_col).value
    src.close()
    print("  已同步生产回复/执行结果 ← 回复输出")


def main():
    pid = os.getpid()
    print("========================================", flush=True)
    print("  [评测] 面谈客户回复", flush=True)
    print(f"  PID: {pid}", flush=True)
    print("========================================", flush=True)
    print(f"  如需手动停止: taskkill /PID {pid}", flush=True)
    print("  正常停止请按 Ctrl+C", flush=True)
    print(flush=True)

    def _atexit_save():
        if _wb_ref[0] is not None:
            try:
                _do_save(_wb_ref[0], label="[退出]")
                _wb_ref[0].close()
            except Exception as e:
                print(f"  [退出保存失败] {e}", flush=True)

    atexit.register(_atexit_save)
    signal.signal(signal.SIGINT, _signal_handler)

    global EVAL_FILE, OUTPUT_FILE, OUTPUT_TMP_FILE, CONFIG_PATH, EVAL_PROMPT_FILE

    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--scenes", "-s", default="",
                        help="instance_id 列表，支持区间。空则处理全部\n例: --scenes=1-3")
    parser.add_argument("--items", default="",
                        help="按「第N条」数据序号过滤（1起，不含表头）\n例: --items=8,9,10 或 --items=8-10")
    parser.add_argument("--input", default="",
                        help="回复输出 xlsx 路径（默认 回复输出目录/回复输出.xlsx）")
    parser.add_argument("--output", default="",
                        help="测评输出 xlsx 路径（默认 测评输出目录/测评输出.xlsx）")
    parser.add_argument("--config", default="",
                        help="配置文件路径（默认 config.json）")
    parser.add_argument("--eval-prompt", default="",
                        help="测评提示词路径（默认 agents/scorer/system.md）")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少条（0=不限制）。试跑可用 --limit 3")
    parser.add_argument("--workers", type=int, default=1,
                        help="并发条数 (默认1；试跑建议1)")
    parser.add_argument("--retries", type=int, default=2, help="失败重试次数 (默认2)")
    parser.add_argument("--force", action="store_true", help="强制重跑已有结果")
    parser.add_argument("--dry-run", action="store_true", help="只打印待执行条目")
    args = parser.parse_args()

    if args.input:
        EVAL_FILE = Path(args.input)
        if not EVAL_FILE.is_absolute():
            EVAL_FILE = SCRIPT_DIR / EVAL_FILE
    if args.output:
        OUTPUT_FILE = Path(args.output)
        if not OUTPUT_FILE.is_absolute():
            OUTPUT_FILE = SCRIPT_DIR / OUTPUT_FILE
        OUTPUT_TMP_FILE = OUTPUT_FILE.with_name(OUTPUT_FILE.stem + "_tmp.xlsx")
    if args.config:
        CONFIG_PATH = Path(args.config)
        if not CONFIG_PATH.is_absolute():
            CONFIG_PATH = SCRIPT_DIR / CONFIG_PATH
    if args.eval_prompt:
        from loop_lib import resolve_prompt_path
        EVAL_PROMPT_FILE = resolve_prompt_path(args.eval_prompt)

    if not EVAL_FILE.exists():
        raise SystemExit(f"回复输出不存在: {EVAL_FILE}（请先运行 run.py）")
    if not EVAL_PROMPT_FILE.exists():
        raise SystemExit(f"测评提示词不存在: {EVAL_PROMPT_FILE}")

    config = load_config(CONFIG_PATH)
    model_cfg = config["model"]
    eval_sys = load_eval_prompt_template()
    requested = parse_scenes_arg(args.scenes) if args.scenes else None
    item_excel_rows = {int(x) + 1 for x in parse_scenes_arg(args.items)} if args.items else None
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"Model:        {model_cfg.get('baseUrl', '')}{model_cfg.get('endpoint', '')} / {model_cfg['modelName']}", flush=True)
    print(f"输入文件:     {EVAL_FILE}", flush=True)
    print(f"输出文件:     {OUTPUT_FILE}", flush=True)
    print(f"配置:         {CONFIG_PATH}", flush=True)
    print(f"测评提示词:   {EVAL_PROMPT_FILE}", flush=True)
    print(f"实例过滤:     {','.join(requested) if requested else '全部'}", flush=True)
    print(f"条目过滤:     {args.items or '无'}", flush=True)
    print(f"条数上限:     {args.limit or '无'}", flush=True)
    print(f"并发:         {args.workers}", flush=True)
    print(f"重试:         {args.retries}", flush=True)
    print(f"强制重跑:     {'是' if args.force else '否'}", flush=True)
    print(flush=True)

    init_log(run_ts)
    wb = init_output()
    _wb_ref[0] = wb
    ws = wb.active
    sync_replies_from_source(ws)
    all_rows = load_rows(ws)
    print(f"  总行数: {len(all_rows)}", flush=True)

    pending: list[tuple[str, int, dict]] = []
    skipped = 0
    for i, row in enumerate(all_rows):
        excel_row = i + 2
        if item_excel_rows is not None and excel_row not in item_excel_rows:
            continue
        sid = row.get(COL["instance_id"], "").strip()
        if not sid:
            continue
        if requested is not None and sid not in requested:
            continue
        has_reply = bool(row.get(COL["reply"])) and is_done(row.get(COL["status"], ""))
        has_score = scores_done(row)
        if not has_reply:
            skipped += 1
            continue
        if has_score and not args.force:
            skipped += 1
            continue
        pending.append((sid, excel_row, row))

    if args.limit and args.limit > 0:
        pending = pending[: args.limit]

    print(f"  跳过(无回复/已完成): {skipped}", flush=True)
    print(f"  待执行: {len(pending)} 条\n", flush=True)

    if not pending:
        print("  无待执行条目（可用 --force 强制重跑，或先运行 run.py 生成回复）", flush=True)
        _wb_ref[0] = None
        wb.close()
        return

    if args.dry_run:
        for sid, ridx, row in pending:
            print(f"  行{ridx}(=第{ridx-1}条) 实例{sid} title={row.get(COL['title'],'')} "
                  f"回复前40字={row.get(COL['reply'],'')[:40]}", flush=True)
        _wb_ref[0] = None
        wb.close()
        return

    base_url = (model_cfg["baseUrl"].rstrip("/")
                + model_cfg["endpoint"].replace("/chat/completions", "").rstrip("/"))
    client = OpenAI(base_url=base_url, api_key=model_cfg["apiKey"], timeout=120)

    t0 = time.time()
    overall = run_batch(
        pending, client, config, eval_sys,
        args.retries, args.force, args.workers, wb, ws,
    )
    print(f"总耗时: {time.time() - t0:.1f}s", flush=True)
    print(f"输出文件: {OUTPUT_FILE}", flush=True)
    print(f"LLM日志:  {_llm_log_file}", flush=True)
    _wb_ref[0] = None
    wb.close()


if __name__ == "__main__":
    main()
