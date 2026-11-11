---
name: facing-scorer
description: 对模拟客户回复做约 17 维 ABCD 打分并给出理由。
---

# Scorer（打分 Agent）

- **输入**：测评集行 + `生产回复`
- **输出**：各维 `_分` / `_理由`、综合得分
- **提示词**：`system.md`（默认）/ `agentforge.md`（平台变体）
- **Toolkit**：`eval.py`
