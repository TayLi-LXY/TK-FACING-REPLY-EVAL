---
name: facing-loop-optimizer
description: >-
  Prompt optimizer for the facing-reply self-evolution loop. Reads PE v1,
  diagnosis, and human annotation; iterates full systemPrompt drafts via
  loop_optimize_io. Use when phase is OPTIMIZING, user asks to 优化提示词/改 PE,
  or after annotation is submitted in TK-FACING-REPLY-EVAL.
---

# 面谈回复提示词优化器

工作目录：`TK-FACING-REPLY-EVAL/`。开始前必须读取上下文：

```bash
python loop_optimize_io.py read [--loop-id ...]
```

可选：`python loop_optimize_io.py read-scorer` 查看评分维度。

完整角色与约束见 `agents/optimizer/system.md`。

## 本地工具对照

| 原工具 | 本地命令 |
| --- | --- |
| `read_optimization_context` | `python loop_optimize_io.py read` |
| `update_pe_draft(content)` | 将**完整**提示词写入文件后：`python loop_optimize_io.py write-draft --file <path>`（或 `--content` / stdin） |
| `read_scorer_config` | `python loop_optimize_io.py read-scorer` |

草稿路径：`loops/<id>/pe/draft.md`。对比时对照 `pe/v1.md`。

## 工作流程

1. `read` 获取 pe_v1、annotation（仅**被采纳**方向）、diagnosis_slim
2. 复述核心问题，与用户确认理解一致
3. 定位 v1 中要改的规则段，说明改什么 / 为什么（锚定校准） / 预计影响
4. 写出**完整**新版 systemPrompt，`write-draft`
5. 等用户反馈；迭代修改再 `write-draft`
6. 用户满意后告知编排侧执行：`python loop_ctl.py adopt-draft`（不要自己跑 v2 除非用户要求）

## 约束

- 只改被优化 Agent 的 systemPrompt，不改评分器/分析器
- `write-draft` 必须是全文，不是片段
- 遵守 diagnosis 中的 `risk_notes`
- 尽量在原段落结构上增改，避免大幅重组
