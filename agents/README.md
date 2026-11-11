# 角色提示词

每个子目录是一个角色：`AGENT.md`（说明）+ 实际注入模型的 `.md`。

| 目录 | 脚本 | 职责 |
| --- | --- | --- |
| `generator/` | `run.py` | 模拟客户回复 |
| `scorer/` | `eval.py` | 多维 ABCD 打分 |
| `analyzer/` | `loop_analyze.py` | 按维诊断 + 汇总 |
| `optimizer/` | `loop_optimize.py` | 改写 PE 草稿 |
| `orchestrator/` | `loop_run.py` / Web | 推进闭环阶段 |
