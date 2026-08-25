import json
import time
import shutil
import tempfile
import argparse
import threading
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import openpyxl
from openpyxl import load_workbook
from openai import OpenAI


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


SCRIPT_DIR = Path(__file__).parent
EVAL_FILE = SCRIPT_DIR / "测评集.csv"
OUTPUT_FILE = SCRIPT_DIR / "回复输出目录" / "回复输出.xlsx"
LOG_DIR = SCRIPT_DIR / "日志"

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
}

_progress_lock = threading.Lock()
_log_lock = threading.Lock()
_llm_log_file = None
_header_to_idx: dict[str, int] = {}


def load_config():
    with open(SCRIPT_DIR / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def render_template(template: str, variables: dict) -> str:
    if not template:
        return ""
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", value or "")
        result = result.replace("${" + key + "}", value or "")
    return result


def parse_history(raw: str) -> list[str]:
    if not raw or not isinstance(raw, str):
        return []
    return [line.strip() for line in raw.split("\n") if line.strip()]


def build_context(lines: list[str]) -> list[dict]:
    msgs = []
    for line in lines:
        if line.startswith("HWP：") or line.startswith("HWP:"):
            n = line.find("：") if "：" in line else line.find(":")
            msgs.append({"role": "user", "content": line[n + 1:].strip()})
        elif line.startswith("C：") or line.startswith("C:"):
            n = line.find("：") if "：" in line else line.find(":")
            msgs.append({"role": "assistant", "content": line[n + 1:].strip()})
    return msgs


def build_messages(sys_prompt: str, context: list[dict], user_msg: str) -> list[dict]:
    return [
        {"role": "system", "content": sys_prompt},
        *context,
        {"role": "user", "content": user_msg.strip() if user_msg else "你好"},
    ]


def init_log(run_ts: str):
    global _llm_log_file
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _llm_log_file = LOG_DIR / f"gen_{run_ts}.jsonl"
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


def _as_xlsx_path(src: Path) -> Path:
    """测评集.csv 实为 xlsx，复制为 .xlsx 供 openpyxl 打开。"""
    if src.suffix.lower() in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        return src
    tmp = Path(tempfile.gettempdir()) / f"facing_eval_{src.stem}.xlsx"
    shutil.copy2(src, tmp)
    return tmp


def init_output() -> openpyxl.Workbook:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not OUTPUT_FILE.exists():
        if not EVAL_FILE.exists():
            raise SystemExit(f"测评集不存在: {EVAL_FILE}")
        src = _as_xlsx_path(EVAL_FILE)
        shutil.copy2(src, OUTPUT_FILE)
        print(f"  测评集已复制到: {OUTPUT_FILE}")
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


def scene_id_of(row: dict) -> str:
    return row.get(COL["instance_id"], "").strip()


def is_done(status: str) -> bool:
    return bool(status) and status.lower() not in ("none", "null", "", "0")


def process_row_data(row: dict, client: OpenAI, config: dict, retries: int,
                     scene_id: str, worker_id: str) -> tuple[str, str, float]:
    agent_cfg = config["agent"]
    model_cfg = config["model"]
    defaults = agent_cfg.get("template_variables", {})

    variables = {
        "customer_name": row.get(COL["customer_name"], "") or defaults.get("customer_name", ""),
        "customer_gender": row.get(COL["customer_gender"], "") or defaults.get("customer_gender", ""),
        "customer_age": row.get(COL["customer_age"], "") or defaults.get("customer_age", ""),
        "customer_personality": row.get(COL["customer_personality"], "") or defaults.get("customer_personality", ""),
        "customer_personality_detail": row.get(COL["customer_personality_detail"], "") or defaults.get("customer_personality_detail", ""),
        "customer_tags": row.get(COL["customer_tags"], "") or defaults.get("customer_tags", ""),
        "customer_extras": row.get(COL["customer_extras"], "") or defaults.get("customer_extras", ""),
        "background": row.get(COL["background"], "") or defaults.get("background", ""),
    }
    history_raw = row.get(COL["history"], "")
    current_msg = row.get(COL["current_msg"], "")
    row_key = f"{scene_id}:{current_msg[:40]}"

    history_lines = parse_history(history_raw)
    sys_prompt = render_template(agent_cfg["system_prompt"], variables)
    context = build_context(history_lines)
    msgs = build_messages(sys_prompt, context, current_msg)

    result = call_llm(client, model_cfg["modelName"], msgs, model_cfg,
                      retries, scene_id, row_key, worker_id)
    cost = result.get("cost_ms", 0)
    if result["success"]:
        return result["reply"], "成功", cost
    return "", f"失败: {result['error']}", cost


def _print_progress(scene_id: str, done: int, total: int,
                    done_overall: int, total_overall: int,
                    scene_ok: int, scene_fail: int,
                    avg_cost: float, overall_start: float):
    bar_len = 15
    filled = bar_len * done // total if total else 0
    bar = "#" * filled + "-" * (bar_len - filled)
    remaining = total_overall - done_overall
    eta_sec = int(remaining * avg_cost / 1000) if avg_cost > 0 else 0
    m, s = divmod(eta_sec, 60)
    eta_str = f"ETA {m:02d}:{s:02d}"
    line = (f"\r  [实例 {scene_id}] [{bar}] {done:02d}/{total:02d} "
            f"ok={scene_ok} fail={scene_fail} "
            f"| 整体: {done_overall:03d}/{total_overall:03d} "
            f"均: {avg_cost / 1000:.1f}s {eta_str}")
    with _progress_lock:
        print(line, end="", flush=True)


def run_scene(scene_id: str, row_indices: list[int], rows_data: list[dict],
              client: OpenAI, config: dict, retries: int, worker_id: str,
              wb_lock: threading.Lock, wb: openpyxl.Workbook, ws,
              overall_counter: dict, cost_samples: list) -> dict:
    reply_col = _header_to_idx[COL["reply"]] + 1
    status_col = _header_to_idx[COL["status"]] + 1
    total = len(row_indices)
    local: list[tuple[int, str, str]] = []
    scene_ok = 0
    scene_fail = 0

    for i, row_idx in enumerate(row_indices, 1):
        reply, status, cost = process_row_data(
            rows_data[row_idx - 2], client, config, retries, scene_id, worker_id)
        local.append((row_idx, reply, status))
        if status == "成功":
            scene_ok += 1
        else:
            scene_fail += 1

        with _progress_lock:
            overall_counter["done"] += 1
            done_overall = overall_counter["done"]
            total_overall = overall_counter["total"]
            overall_start = overall_counter["start"]
            cost_samples.append(cost)
            avg_cost = sum(cost_samples) / len(cost_samples)

        _print_progress(scene_id, i, total, done_overall, total_overall,
                        scene_ok, scene_fail, avg_cost, overall_start)

    with wb_lock:
        for row_idx, reply, status in local:
            ws.cell(row=row_idx, column=reply_col).value = reply
            ws.cell(row=row_idx, column=status_col).value = status
        wb.save(OUTPUT_FILE)

    with _progress_lock:
        print(f"\n  [实例 {scene_id}] 完成: ok={scene_ok} fail={scene_fail}  → 已保存")
    return {"scene_id": scene_id, "ok": scene_ok, "fail": scene_fail}


def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--scenes", "-s", default="",
                        help="instance_id 列表，支持区间和逗号分隔\n例: --scenes=1-10 或 --scenes=1,3,5")
    parser.add_argument("--items", default="",
                        help="按「第N条」数据序号过滤（1起，不含表头）\n例: --items=8,9,10 或 --items=8-10")
    parser.add_argument("--workers", type=int, default=3,
                        help="并发实例数 (默认3，每个 instance_id 一个线程)")
    parser.add_argument("--retries", type=int, default=2, help="失败重试次数 (默认2)")
    parser.add_argument("--force", action="store_true",
                        help="强制重跑本次指定的实例/条目（覆盖已有结果）")
    parser.add_argument("--dry-run", action="store_true", help="只打印待执行实例，不实际执行")
    args = parser.parse_args()

    if not args.scenes and not args.items:
        raise SystemExit("请指定 --scenes 或 --items")

    config = load_config()
    model_cfg = config["model"]
    agent_cfg = config["agent"]

    requested = parse_scenes_arg(args.scenes) if args.scenes else None
    # 第N条 → Excel 行号 = N + 1
    item_excel_rows = {int(x) + 1 for x in parse_scenes_arg(args.items)} if args.items else None
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("========================================")
    print("  [本地生成] 面谈客户回复 - 直接调用 LLM")
    print("========================================")
    print(f"Agent:        {agent_cfg['agent_name']} ({agent_cfg['code']})")
    print(f"Model:        {model_cfg.get('baseUrl', '')}{model_cfg.get('endpoint', '')} / {model_cfg['modelName']}")
    print(f"测评集:       {EVAL_FILE}")
    print(f"输出文件:     {OUTPUT_FILE}")
    print(f"实例过滤:     {','.join(requested) if requested else '全部(按条目)'}")
    print(f"条目过滤:     {args.items or '无'}")
    print(f"并发实例数:   {args.workers}")
    print(f"重试次数:     {args.retries}")
    print(f"强制重跑:     {'是' if args.force else '否'}")
    print()

    init_log(run_ts)

    wb = init_output()
    ws = wb.active

    all_row_dicts = load_rows(ws)
    print(f"  测评集总行数: {len(all_row_dicts)}")

    for required in (COL["reply"], COL["status"], COL["instance_id"]):
        if required not in _header_to_idx:
            raise SystemExit(f"输出表缺少列: {required}")

    scene_groups: dict[str, list[int]] = {}
    for i, row in enumerate(all_row_dicts):
        excel_row = i + 2
        if item_excel_rows is not None and excel_row not in item_excel_rows:
            continue
        sid = scene_id_of(row)
        if requested is not None and sid not in requested:
            continue
        if sid:
            scene_groups.setdefault(sid, []).append(excel_row)

    print(f"  命中实例数: {len(scene_groups)}")
    print()

    pending = []
    skipped = []
    for sid in sorted(scene_groups.keys(), key=lambda x: int(x) if x.isdigit() else x):
        row_indices = scene_groups[sid]
        if not args.force and all(
            is_done(all_row_dicts[r - 2].get(COL["status"], ""))
            for r in row_indices
        ):
            skipped.append(sid)
        else:
            # 强制或未完成时：仅重跑未完成行；--force 则全部
            if args.force:
                pending.append((sid, row_indices))
            else:
                todo = [r for r in row_indices
                        if not is_done(all_row_dicts[r - 2].get(COL["status"], ""))]
                if todo:
                    pending.append((sid, todo))
                else:
                    skipped.append(sid)

    if skipped:
        print(f"  [跳过] 已有结果: {len(skipped)} 组")
    print(f"  待执行: {len(pending)} 组\n")

    if not pending:
        print("  无待执行条目（用 --force 可强制重跑）")
        wb.close()
        return

    if args.dry_run:
        for sid, rows in pending:
            items = ",".join(str(r - 1) for r in rows)
            print(f"  实例 {sid}: {len(rows)} 条 Excel行{rows} (=第{items}条)")
        wb.close()
        return

    base_url = (model_cfg["baseUrl"].rstrip("/")
                + model_cfg["endpoint"].replace("/chat/completions", "").rstrip("/"))
    client = OpenAI(base_url=base_url, api_key=model_cfg["apiKey"], timeout=120)

    wb_lock = threading.Lock()
    total_rows = sum(len(rows) for _, rows in pending)
    overall_counter = {"done": 0, "total": total_rows, "start": time.time()}
    cost_samples: list[float] = []
    main_t0 = time.time()

    scene_results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                run_scene, sid, rows, all_row_dicts, client, config,
                args.retries, f"worker-{i}", wb_lock, wb, ws,
                overall_counter, cost_samples
            ): sid
            for i, (sid, rows) in enumerate(pending)
        }
        for future in as_completed(futures):
            sid = futures[future]
            try:
                res = future.result()
                scene_results.append(res)
            except Exception as e:
                print(f"\n  [实例 {sid}] 异常: {e}")
                scene_results.append({"scene_id": sid, "ok": 0, "fail": 0, "error": str(e)})

    print()
    g_ok = sum(r.get("ok", 0) for r in scene_results)
    g_fail = sum(r.get("fail", 0) for r in scene_results)
    print(f"\n======== 全部完成 ========")
    print(f"实例数: {len(scene_results)}")
    print(f"成功: {g_ok}  失败: {g_fail}")
    print(f"总耗时: {time.time() - main_t0:.1f}s")
    print(f"输出文件: {OUTPUT_FILE}")
    print(f"LLM日志:  {_llm_log_file}")
    wb.close()


if __name__ == "__main__":
    main()
