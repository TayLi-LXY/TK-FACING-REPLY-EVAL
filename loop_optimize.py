#!/usr/bin/env python3
"""PE 草稿自动优化（供终端 loop_run 与 Web 共用）。"""

from __future__ import annotations

import json
from typing import Callable

from loop_lib import (
    OPTIMIZER_PE,
    LoopPaths,
    call_llm,
    load_config,
    load_json,
    make_openai_client,
)


def strip_md_fences(text: str) -> str:
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def auto_optimize_draft(
    paths: LoopPaths,
    log: Callable[[str], None] | None = None,
) -> str:
    """基于 annotation + diagnosis 调用 LLM，写入 pe/draft.md，返回草稿全文。"""
    _log = log or (lambda _m: None)
    pe_v1 = paths.pe_v1.read_text(encoding="utf-8")
    annotation = load_json(paths.annotation) if paths.annotation.exists() else {}
    diagnosis = load_json(paths.diagnosis) if paths.diagnosis.exists() else {}
    opt_prompt = OPTIMIZER_PE.read_text(encoding="utf-8")
    user = (
        "请基于以下校准结论，输出**完整**新版 systemPrompt 全文（不要 markdown 围栏，不要解释）。\n"
        "只改被采纳的方向；保持原有段落结构。\n\n"
        f"## 校准\n{json.dumps(annotation, ensure_ascii=False, indent=2)}\n\n"
        f"## 诊断摘要\n{json.dumps(diagnosis.get('overall_assessment') or {}, ensure_ascii=False, indent=2)}\n\n"
        f"## 当前提示词 v1\n{pe_v1}\n"
    )
    cfg = load_config(paths.runtime_config if paths.runtime_config.exists() else None)
    model_cfg = {
        **cfg["model"],
        "temperature": 0.3,
        "maxTokens": max(int(cfg["model"].get("maxTokens") or 2048), 8192),
    }
    client = make_openai_client(model_cfg)
    _log("正在调用 LLM 生成 PE 草稿...")
    result = call_llm(
        client,
        model_cfg["modelName"],
        [
            {"role": "system", "content": opt_prompt},
            {"role": "user", "content": user},
        ],
        model_cfg,
        retries=2,
    )
    if not result["success"]:
        raise RuntimeError(f"自动优化失败: {result.get('error')}（可改用手工编辑模式）")
    text = strip_md_fences(result["reply"] or "")
    paths.pe_draft.write_text(text, encoding="utf-8")
    _log(f"已写入草稿: {paths.pe_draft} ({len(text)} chars)")
    return text
