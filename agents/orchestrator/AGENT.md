---
name: facing-orchestrator
description: 驱动自进化闭环 8 阶段状态机，并在门控处停等人工。
---

# Orchestrator（编排 Agent）

- **职责**：推进 phase，不直接改 PE
- **提示词**：`system.md`（平台工具风格说明）
- **本地执行真相**：`loop_run.py`（终端）/ `web/backend/orchestrator.py`（Web）
- **HITL**：WAITING_ANNOTATION、COMPARING（及 Web 上的 OPTIMIZING 停等）
