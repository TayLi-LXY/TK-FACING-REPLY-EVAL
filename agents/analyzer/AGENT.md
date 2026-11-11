---
name: facing-analyzer
description: 对 v1 打分结果做 MapReduce 深钻诊断，产出可校准的优化方向。
---

# Analyzer（诊断 Agent 组）

1. **Map** — `dimension.md`：每个评分维度一个 worker，分析 C/D 案例与改进方向
2. **Reduce** — `synthesizer.md`：跨维汇总 → `overall_assessment`
3. **归档** — `archive_monolithic.md`：旧版单次全量分析器（仅文档，运行时不加载）

- **Toolkit**：`loop_analyze.py`
- **产物**：`reports/dim_analyses.json`、`reports/diagnosis.json`
