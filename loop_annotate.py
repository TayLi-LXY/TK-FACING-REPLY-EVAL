#!/usr/bin/env python3
"""人工校准落盘：写入 annotation.json，推进到 OPTIMIZING。

⚠️ 人工门控：仅在用户明确确认后由编排 Agent 调用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from loop_lib import (
    LoopPaths,
    SCRIPT_DIR,
    now_iso,
    read_state,
    require_confirmation,
    resolve_loop_id,
    save_json,
    set_phase,
)


def submit_annotation(
    loop_id: str = "",
    payload: dict[str, Any] | None = None,
    *,
    confirmed: bool = False,
) -> dict[str, Any]:
    """写入 annotation.json 并进入 OPTIMIZING。须 confirmed=True。"""
    require_confirmation(confirmed=confirmed, action="提交人工校准")

    loop_id = resolve_loop_id(loop_id)
    state = read_state(loop_id)
    paths = LoopPaths(loop_id)
    payload = dict(payload or {})

    if not payload.get("adopted_directions") and paths.diagnosis.exists():
        diag = json.loads(paths.diagnosis.read_text(encoding="utf-8"))
        oa = diag.get("overall_assessment") or {}
        opt = oa.get("optimization_priority")
        if isinstance(opt, list) and opt:
            payload["adopted_directions"] = opt
            payload.setdefault(
                "business_summary",
                oa.get("quality_diagnosis") or oa.get("key_bottleneck") or "",
            )

    payload["loop_id"] = loop_id
    payload["confirmed_at"] = now_iso()
    payload["source_diagnosis"] = str(paths.diagnosis)

    save_json(paths.annotation, payload)
    set_phase(state, "OPTIMIZING")
    return {
        "ok": True,
        "loop_id": loop_id,
        "phase": "OPTIMIZING",
        "annotation": str(paths.annotation),
        "adopted_count": len(payload.get("adopted_directions") or []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="提交人工校准（门控）")
    parser.add_argument("--loop-id", default="")
    parser.add_argument("--summary", default="", help="业务总评语")
    parser.add_argument(
        "--adopted",
        default="",
        help='采纳的优化方向 JSON 数组字符串，例: [{"rank":1,"direction":"..."}]',
    )
    parser.add_argument(
        "--rejected",
        default="",
        help="驳回的方向 JSON 数组字符串",
    )
    parser.add_argument("--from-file", default="", help="从 JSON 文件读取完整 annotation")
    parser.add_argument(
        "--confirmed",
        action="store_true",
        help="确认用户已口头/书面确认（与 --interactive 二选一）",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="终端交互确认（推荐；会提示后自动加确认）",
    )
    args = parser.parse_args()

    if args.interactive and not args.confirmed:
        print("===== 提交人工校准（终端确认）=====")
        print("将写入 annotation 并进入 OPTIMIZING。")
        ans = input("确认提交？(y/N): ").strip().lower()
        if ans not in ("y", "yes", "是", "确认"):
            raise SystemExit("已取消")
        args.confirmed = True

    if not args.confirmed:
        raise SystemExit(
            "拒绝执行：缺少 --confirmed 或 --interactive。须先确认后再提交校准。"
        )

    if args.from_file:
        src = Path(args.from_file)
        if not src.is_absolute():
            src = SCRIPT_DIR / src
        payload = json.loads(src.read_text(encoding="utf-8"))
    else:
        adopted = json.loads(args.adopted) if args.adopted else []
        rejected = json.loads(args.rejected) if args.rejected else []
        if not isinstance(adopted, list):
            raise SystemExit("--adopted 必须是 JSON 数组")
        payload = {
            "business_summary": args.summary,
            "adopted_directions": adopted,
            "rejected_directions": rejected,
        }

    result = submit_annotation(args.loop_id, payload, confirmed=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
