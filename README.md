# 面谈客户回复评测

对「模拟客户回复」Agent 做批量生成与多维度打分评测。场景为保险陪练：健康财富规划师（HWP）与潜在客户（C）面谈，模型扮演客户给出回复，再由打分模型按 ABCD 档逐维评分。

## 流程概览

```
测评集.csv（或抽样版）
        │
        ▼
   sample_eval_set.py   （可选）分层抽样 + 脏样本过滤
        │
        ▼
      run.py            按测评集调用回复模型 → 回复输出目录/回复输出.xlsx
        │
        ▼
      eval.py           按测评提示词打分 → 测评输出目录/测评输出.xlsx
```

## 环境准备

- Python 3.10+（建议）
- 依赖：`openpyxl`、`openai`

```bash
pip install openpyxl openai
```

复制配置并填入密钥（`config.json` 已在 `.gitignore` 中，勿提交）：

```bash
cp config.example.json config.json
```

主要配置项在 `config.json`：

| 段 | 说明 |
| --- | --- |
| `model` | 模型名、baseUrl、apiKey、endpoint、temperature 等 |
| `agent` | 被评 Agent 名、`system_prompt`（含 `{{customer_name}}` 等占位符） |

回复生成提示词也可对照 `面课回复生成提示词.md`；打分维度与规则见 `测评提示词.md`。

## 目录与产物

| 路径 | 说明 |
| --- | --- |
| `测评集.csv` | 初版测评集（扩展名虽为 csv，实际为 xlsx） |
| `测评集_抽样版.xlsx` | 抽样脚本产出（数据大表 / 质量说明 / 行数统计） |
| `回复输出目录/回复输出.xlsx` | `run.py` 写入的生产回复与执行结果 |
| `测评输出目录/测评输出.xlsx` | `eval.py` 写入的各维分数与理由 |
| `日志/` | LLM 调用日志（jsonl） |
| `测评集构建Skill.md` | 评测集构建方法论（抽样、过滤、质量检查等） |

## 1. 抽样（可选）

从初版测评集按 `title × instance_id` 分层抽样，并标记脏样本。

```bash
python sample_eval_set.py
python sample_eval_set.py --per-instance 8 --seed 42
python sample_eval_set.py --include-dirty   # 脏样本一并写入数据大表
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--input` | `测评集.csv` | 输入路径 |
| `--output` | `测评集_抽样版.xlsx` | 输出路径 |
| `--per-instance` | `8` | 每层最多抽几条 |
| `--seed` | `42` | 随机种子，保证可复现 |
| `--include-dirty` | 关 | 是否把脏样本写入数据大表 |

脏样本规则（命中则不进主集）：`脏_background`、`脏_hwp_过短`、`缺关键输入`。`生产回复为空` 仅记账，不因此剔除。

> `run.py` 默认读根目录 `测评集.csv`。若要用抽样版，请自行改脚本输入路径，或把抽样结果拷成当前输入文件。

## 2. 批量生成回复

```bash
python run.py
python run.py --scenes=1-10
python run.py --items=8-10 --workers 3
python run.py --force --dry-run
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--scenes` / `-s` | 全部 | 按 `instance_id` 过滤，支持 `1-10` 或 `1,3,5` |
| `--items` | 无 | 按数据行序号过滤（从 1 起，不含表头） |
| `--workers` | `3` | 并发实例数 |
| `--retries` | `2` | 失败重试次数 |
| `--force` | 关 | 强制重跑本次指定条目 |
| `--dry-run` | 关 | 只打印待执行项，不调模型 |

已有「执行结果」成功的行默认跳过；输出写入 `回复输出目录/回复输出.xlsx`。

## 3. 多维打分

需先有回复输出，且存在 `测评提示词.md`。

```bash
python eval.py
python eval.py --limit 3 --workers 1          # 试跑
python eval.py --scenes=1-3 --force
python eval.py --items=8,9,10 --dry-run
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--scenes` / `-s` | 全部 | 按 `instance_id` 过滤 |
| `--items` | 无 | 按数据行序号过滤 |
| `--limit` | `0` | 最多处理条数，`0` 不限制 |
| `--workers` | `1` | 并发条数（试跑建议 1） |
| `--retries` | `2` | 失败重试 |
| `--force` | 关 | 强制重跑已有分数 |
| `--dry-run` | 关 | 只打印待执行条目 |

无成功回复或已完成打分的行默认跳过。输出写入 `测评输出目录/测评输出.xlsx`。

打分维度示例（ABCD 四档）：主题相关性、话题边界守护、人设信息忠实度、口语化程度、流程推进被动性、HWP 跟随克制度等，完整定义见 `测评提示词.md`。

## 数据列要点

测评集核心输入列需与 Agent / 打分 PE 对齐，主要包括：

- 场景：`instance_id`、`title`、`target`、`background`
- 客户：`customer_name`、`customer_gender`、`customer_age`、`customer_personality`、`customer_personality_detail`、`customer_tags`、`customer_extras`
- 对话：`history_messages`、`current_hwp_message`
- 待评输出：`生产回复`（由 `run.py` 填充）
- 执行态：`执行结果`；打分结果为各维度 `_分` / `_理由` 列

溯源与违规类元数据只用于管理，**不得拼进判定/打分模型输入**（防泄漏）。

## 相关文档

- `测评集构建Skill.md` — 评测集形态选型、抽样覆盖、过滤记账与质量检查清单
- `测评提示词.md` — 打分 PE 与维度细则
- `面课回复生成提示词.md` — 客户回复生成侧提示词参考
- `agentforge平台测评集/` — 平台侧测评集与提示词副本

## 注意事项

1. 不要把含真实 `apiKey` 的 `config.json` 提交到仓库。
2. 中断 `eval.py` 时会尽量落盘临时结果；正式结果在 `测评输出目录`。
3. 构建/抽样脚本应可重复执行；改规则后建议全量重跑并核对质量说明 sheet。
