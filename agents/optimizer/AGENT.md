---
name: facing-optimizer
description: 根据人工校准结论改写完整 systemPrompt 草稿。
---

# Optimizer（PE 优化 Agent）

- **输入**：`annotation.json` + 诊断摘要 + `pe/v1.md`
- **输出**：完整新提示词 → `pe/draft.md`
- **提示词**：`system.md`
- **Toolkit**：`loop_optimize.py` / `loop_optimize_io.py`
- **约束**：只改被采纳方向；HITL 采纳草稿后才进 RUNNING_V2
