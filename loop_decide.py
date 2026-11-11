#!/usr/bin/env python3
"""最终决策：ADOPTED / ROLLED_BACK（人工门控）。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from typing import Any

from loop_lib import (
    CONFIG_FILE,
    LoopPaths,
    load_config,
    now_iso,
    read_state,
    require_confirmation,
    resolve_loop_id,
    save_json,
    set_phase,
    write_runtime_config,
    write_state,
)


def apply_decision(
    loop_id: str = "",
    decision: str = "",
    note: str = "",
    *,
    confirmed: bool = False,
    write_config: bool = True,
) -> dict[str, Any]:
    """执行最终决策并进入 DONE。须 confirmed=True。"""
    require_confirmation(confirmed=confirmed, action="最终采纳/回滚决策")
    if decision not in ("ADOPTED", "ROLLED_BACK"):
        raise ValueError("decision 须为 ADOPTED 或 ROLLED_BACK")

    loop_id = resolve_loop_id(loop_id)
    state = read_state(loop_id)
    paths = LoopPaths(loop_id)

    decision_payload: dict[str, Any] = {
        "decision": decision,
        "note": note,
        "decided_at": now_iso(),
    }

    if decision == "ADOPTED":
        pe_src = paths.pe_v2 if paths.pe_v2.exists() else paths.pe_draft
        if not pe_src.exists():
            raise FileNotFoundError("无 v2/draft PE 可采纳")
        pe_text = pe_src.read_text(encoding="utf-8")
        if CONFIG_FILE.exists() and write_config:
            bak = CONFIG_FILE.with_suffix(".json.bak")
            shutil.copy2(CONFIG_FILE, bak)
            cfg = load_config()
            cfg["agent"]["system_prompt"] = pe_text
            save_json(CONFIG_FILE, cfg)
            decision_payload["config_backup"] = str(bak)
            decision_payload["config_written"] = str(CONFIG_FILE)
        decision_payload["pe_adopted"] = str(pe_src)
        state["pe"]["active"] = "v2"
    else:
        if paths.pe_v1.exists():
            write_runtime_config(paths, paths.pe_v1.read_text(encoding="utf-8"))
        state["pe"]["active"] = "v1"

    state["decision"] = decision_payload
    set_phase(state, "DONE")
    write_state(state)

    if paths.memory_file.exists():
        mem = paths.memory_file.read_text(encoding="utf-8")
        mem += f"\n- decision: {decision} @ {decision_payload['decided_at']} {note}\n"
        paths.memory_file.write_text(mem, encoding="utf-8")

    return {
        "ok": True,
        "loop_id": loop_id,
        "phase": "DONE",
        "decision": decision_payload,
        "stats": state.get("stats"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="闭环最终决策（门控）")
    parser.add_argument("--loop-id", default="")
    parser.add_argument(
        "--decision",
        default="",
        choices=["ADOPTED", "ROLLED_BACK", ""],
        help="ADOPTED=采纳 v2 写回 config；ROLLED_BACK=回滚（--interactive 时可省略）",
    )
    parser.add_argument(
        "--confirmed",
        action="store_true",
        help="确认用户已明确表态（与 --interactive 二选一）",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="终端交互选择 ADOPTED / ROLLED_BACK",
    )
    parser.add_argument("--note", default="", help="决策备注")
    parser.add_argument(
        "--write-config",
        action="store_true",
        default=True,
        help="ADOPTED 时写回 config.json（默认开）",
    )
    parser.add_argument("--no-write-config", action="store_true", help="不写回 config.json")
    args = parser.parse_args()

    if args.interactive:
        print("===== 最终决策（终端确认）=====")
        print("1 = ADOPTED（采纳 v2）")
        print("2 = ROLLED_BACK（回滚）")
        ans = input("请选择 [1/2]: ").strip()
        if ans.startswith("2") or ans.upper().startswith("R"):
            args.decision = "ROLLED_BACK"
        else:
            args.decision = "ADOPTED"
        if not args.note:
            args.note = input("备注（可空）: ").strip()
        conf = input(f"确认执行 {args.decision}？(y/N): ").strip().lower()
        if conf not in ("y", "yes", "是", "确认", "采纳", "回滚"):
            raise SystemExit("已取消")
        args.confirmed = True

    if not args.decision:
        raise SystemExit("请指定 --decision ADOPTED|ROLLED_BACK，或使用 --interactive")

    if not args.confirmed:
        raise SystemExit(
            "拒绝执行：缺少 --confirmed 或 --interactive。须先确认后再 decide。"
        )

    result = apply_decision(
        args.loop_id,
        args.decision,
        args.note,
        confirmed=True,
        write_config=not args.no_write_config,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
