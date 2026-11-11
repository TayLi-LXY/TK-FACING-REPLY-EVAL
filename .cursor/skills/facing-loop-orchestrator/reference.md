# 工具速查（终端优先）

在仓库根 `TK-FACING-REPLY-EVAL` 下执行。门控用终端，不要用 Cursor 聊天代替确认。

## 一键

```bash
python loop_run.py --dataset 测评集_抽样版.xlsx --scenes=5,18,27
python loop_run.py --loop-id <id>
python loop_run.py --loop-id <id> --from-phase WAITING_ANNOTATION
```

## 分步门控

```bash
python loop_annotate.py --interactive
python loop_decide.py --interactive
```
