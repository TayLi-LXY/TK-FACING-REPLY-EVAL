---
name: facing-loop-orchestrator
description: >-
  Helps with the facing-reply self-evolution loop. Primary UX is terminal
  loop_run.py with input() gates. Use when user mentions 自进化闭环、loop_run、
  终端门控、面谈回复评测闭环, or needs status/debug of loops in TK-FACING-REPLY-EVAL.
---

# 面谈回复自进化编排（终端优先）

**主交互在终端，不在 Cursor 对话框。**

工作目录：`TK-FACING-REPLY-EVAL/`。

## 推荐用法（给用户看的命令）

新建并全程终端门控：

```bash
python loop_run.py --dataset 测评集_抽样版.xlsx --scenes=5,18,27
```

续跑已有 loop（从当前 `state.json` 阶段继续，门控仍在终端）：

```bash
python loop_run.py --loop-id 20260904_171908
```

从指定阶段续跑：

```bash
python loop_run.py --loop-id 20260904_171908 --from-phase WAITING_ANNOTATION
```

单独门控：

```bash
python loop_annotate.py --interactive
python loop_decide.py --interactive
```

## Agent 在对话里该做什么

1. **不要**在聊天里代替用户做「确认/采纳」门控。
2. 需要推进闭环时：告诉用户在**本机终端**执行 `python loop_run.py ...`，并说明会在终端出现 `input` 提示。
3. 可只做辅助：`python loop_ctl.py status`、读 `reports/*.json`、解释失败原因、改代码。
4. 若用户明确要求你代跑且**跳过门控**，须拒绝门控步骤，改提示他们用终端 `loop_run.py`。

## 工具对照

| 能力 | 命令 |
| --- | --- |
| 一键闭环（终端 HITL） | `python loop_run.py` |
| 状态 | `python loop_ctl.py status` |
| 单步校准门控 | `python loop_annotate.py -i` |
| 单步决策门控 | `python loop_decide.py -i` |

编排/优化提示词见：`agents/orchestrator/`、`agents/optimizer/`；本地执行以 `loop_run.py` 为准。
