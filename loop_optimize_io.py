#!/usr/bin/env python3
"""优化器 I/O：read_context / write_draft / read_scorer。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from loop_lib import SCORER_PE, LoopPaths, read_state, resolve_loop_id


def cmd_read(args: argparse.Namespace) -> None:
    loop_id = resolve_loop_id(args.loop_id)
    state = read_state(loop_id)
    paths = LoopPaths(loop_id)

    pe_v1 = paths.pe_v1.read_text(encoding="utf-8") if paths.pe_v1.exists() else ""
    draft = paths.pe_draft.read_text(encoding="utf-8") if paths.pe_draft.exists() else pe_v1
    annotation = {}
    diagnosis = {}
    if paths.annotation.exists():
        annotation = json.loads(paths.annotation.read_text(encoding="utf-8"))
    if paths.diagnosis.exists():
        diagnosis = json.loads(paths.diagnosis.read_text(encoding="utf-8"))

    # 精简诊断：给优化器 overall + 高优维度摘要
    diag_slim = {
        "overall_assessment": diagnosis.get("overall_assessment"),
        "avg_score": diagnosis.get("avg_score"),
        "sample_count": diagnosis.get("sample_count"),
        "dimensions_top": sorted(
            [
                {
                    "dimension_name": d.get("dimension_name"),
                    "priority": d.get("priority"),
                    "priority_reason": d.get("priority_reason"),
                    "improvement_direction": d.get("improvement_direction"),
                    "low_score_problems": (d.get("low_score_problems") or {}).get("summary"),
                }
                for d in (diagnosis.get("dimensions_analysis") or [])
                if isinstance(d, dict)
            ],
            key=lambda x: x.get("priority") if isinstance(x.get("priority"), int) else 999,
        )[:8],
        "risk_notes": (diagnosis.get("overall_assessment") or {}).get("risk_notes"),
    }

    out = {
        "loop_id": loop_id,
        "phase": state.get("phase"),
        "pe_v1": pe_v1,
        "pe_draft": draft,
        "annotation": annotation,
        "diagnosis_slim": diag_slim,
        "paths": {
            "pe_v1": str(paths.pe_v1),
            "pe_draft": str(paths.pe_draft),
            "annotation": str(paths.annotation),
            "diagnosis": str(paths.diagnosis),
        },
    }
    if args.out:
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": True, "written": args.out}, ensure_ascii=False))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_write_draft(args: argparse.Namespace) -> None:
    loop_id = resolve_loop_id(args.loop_id)
    paths = LoopPaths(loop_id)
    if args.file:
        src = Path(args.file)
        if not src.is_absolute():
            src = Path(__file__).parent / src
        content = src.read_text(encoding="utf-8")
    elif args.content:
        content = args.content
    else:
        # stdin
        content = sys.stdin.read()
    if not content.strip():
        raise SystemExit("草稿内容为空")
    paths.pe_draft.write_text(content, encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "loop_id": loop_id,
        "draft": str(paths.pe_draft),
        "chars": len(content),
    }, ensure_ascii=False, indent=2))


def cmd_read_scorer(_: argparse.Namespace) -> None:
    prompt = SCORER_PE.read_text(encoding="utf-8")
    marker = "# 待评测数据"
    if marker in prompt:
        prompt = prompt[: prompt.index(marker)].rstrip()
    print(prompt)


def main() -> None:
    parser = argparse.ArgumentParser(description="优化器读写工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_read = sub.add_parser("read", help="读取优化上下文")
    p_read.add_argument("--loop-id", default="")
    p_read.add_argument("--out", default="", help="写入文件而非 stdout")
    p_read.set_defaults(func=cmd_read)

    p_write = sub.add_parser("write-draft", help="写入完整 PE 草稿")
    p_write.add_argument("--loop-id", default="")
    p_write.add_argument("--file", default="", help="从文件读完整提示词")
    p_write.add_argument("--content", default="", help="直接传内容（短文本）")
    p_write.set_defaults(func=cmd_write_draft)

    p_scorer = sub.add_parser("read-scorer", help="读取评分器维度定义")
    p_scorer.set_defaults(func=cmd_read_scorer)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
