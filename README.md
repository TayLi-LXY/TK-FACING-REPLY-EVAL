# Facing Reply Loop

保险面谈陪练里「模拟客户回复」的批量评测与提示词自进化闭环。

健康财富规划师（HWP）与潜在客户面谈时，模型扮演客户给出下一条回复。本仓库把这条回复批量跑出来，按约 17 个维度打 ABCD 档，再经深钻诊断、人工校准、改提示词、复跑对比，决定采纳或回滚。

仓库地址：[github.com/TayLi-LXY/TK-FACING-REPLY-EVAL](https://github.com/TayLi-LXY/TK-FACING-REPLY-EVAL)

## 它做什么

```
测评集（可抽样）
    → 生成客户回复
    → 多维 ABCD 打分
    → 按维度深钻诊断
    → 人工确认优化方向
    → 改写提示词并复跑
    → 对比 v1 / v2，采纳或回滚
```

阶段：

```
RUNNING_V1 → DEEP_DIVING → WAITING_ANNOTATION
  → OPTIMIZING → RUNNING_V2 → COMPARING → DONE
```

跑批失败会进入 `FAILED`。校准和最终采纳必须人工确认，脚本不会静默改线上提示词。

## 角色

| 角色 | 提示词 | 脚本 | 职责 |
| --- | --- | --- | --- |
| Generator | `agents/generator/` | `run.py` | 扮演客户，写出 `生产回复` |
| Scorer | `agents/scorer/` | `eval.py` | 约 17 维 ABCD 打分并给理由 |
| Analyzer | `agents/analyzer/` | `loop_analyze.py` | 按维诊断，再汇总成优化方向 |
| Annotator | — | `loop_annotate.py` | 人确认采纳或驳回方向 |
| Optimizer | `agents/optimizer/` | `loop_optimize.py` | 按校准结论改写完整提示词草稿 |
| Comparer / Decider | — | `loop_compare.py` / `loop_decide.py` | 对比 v1、v2，采纳或回滚 |
| Orchestrator | `agents/orchestrator/` | `loop_run.py`、`web/` | 推进阶段，到门控就停 |

Scorer 分两块：业务合规（跑题、人设、跟话术、关键词追问等）和拟人自然度（语气、口语、立场、长度）。判分规则是「默认 B，有正向证据才升 A」。

## 目录

| 路径 | 说明 |
| --- | --- |
| `agents/` | 各角色提示词 |
| `skills/dataset-build/` | 测评集抽样与脏样本规则 |
| `loops/<id>/` | 一次闭环的状态、提示词版本、报告和跑批结果 |
| `run.py` / `eval.py` | 单独批量生成 / 打分 |
| `loop_run.py` / `loop_*.py` | 闭环编排与分步命令 |
| `web/` | 浏览器工作台（FastAPI + React） |
| `agentforge平台测评集/` | 平台侧测评集与评分器副本 |

一次闭环的工作区：

```
loops/<loop_id>/
  state.json
  MEMORY.md
  pe/v1.md | draft.md | v2.md
  reports/diagnosis.json | annotation.json | compare.json
  runs/dataset.xlsx | v1_*.xlsx | v2_*.xlsx
```

## 环境

- Python 3.10+
- Node.js（只用 Web 时需要）

```bash
pip install openpyxl openai
pip install -r requirements-web.txt
cp config.example.json config.json
```

在 `config.json` 里填写模型名、`baseUrl` 和 `apiKey`。这个文件已在 `.gitignore` 中，不要提交。

## 用法

抽样（可选，分层 + 脏样本过滤，种子固定可复现）：

```bash
python sample_eval_set.py
python sample_eval_set.py --per-instance 8 --seed 42
```

一键闭环（终端里确认校准和最终决策）：

```bash
python loop_run.py --dataset 测评集_抽样版.xlsx --scenes=5,18,27
python loop_run.py --loop-id <id>
python loop_ctl.py status
```

单独跑生成和打分：

```bash
python run.py --scenes=5,18,27 --workers 3
python eval.py --scenes=5,18,27
```

Web：

```bash
python -m uvicorn web.backend.main:app --reload --host 127.0.0.1 --port 8765
cd web/frontend && npm install && npm run dev
```

浏览器打开 http://127.0.0.1:5173 ，接口由前端代理到 8765。

## 注意

1. 不要提交带真实密钥的 `config.json`。
2. 校准与采纳/回滚需要 `--confirmed`、`--interactive`，或在 Web 上确认。
3. 模型返回 `403 FreeTierOnly` 时是额度用尽，不是打分逻辑错误；先处理账户额度再新开一轮。
4. 历史文件名（如 `测评提示词.md`）由 `loop_lib.resolve_prompt_path` 映射到 `agents/`。
