"""
从初版测评集.csv（实为 xlsx）分层抽样，产出三 sheet：
  数据大表 / 质量说明 / 行数统计

不做从生产源回流的构建；不覆盖初版文件。
"""

from __future__ import annotations

import argparse
import random
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl
from openpyxl import Workbook

SCRIPT_DIR = Path(__file__).parent
DEFAULT_INPUT = SCRIPT_DIR / "测评集.csv"
DEFAULT_OUTPUT = SCRIPT_DIR / "测评集_抽样版.xlsx"

# 命中任一条则标为脏样本，默认不进抽样主表
DIRTY_RULES = ("脏_background", "脏_hwp_过短", "缺关键输入")
# 仅记账，不因此剔除
INFO_RULES = ("生产回复为空",)

BG_NOISE_TOKENS = ("退款",)
BG_TRIVIAL = frozenset({"", "无", "无。", "无！", "无?"})

META_COLS = ("样本分层", "质量标记")


def as_xlsx_path(src: Path) -> Path:
    """测评集.csv 实为 xlsx，复制为 .xlsx 供 openpyxl 打开。"""
    if src.suffix.lower() in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        return src
    tmp = Path(tempfile.gettempdir()) / f"facing_sample_{src.stem}.xlsx"
    shutil.copy2(src, tmp)
    return tmp


def cell_str(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def load_source(path: Path) -> tuple[list[str], list[dict]]:
    xlsx = as_xlsx_path(path)
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header_row = next(rows_iter)
    headers = [cell_str(h) if h is not None else "" for h in header_row]
    # 去掉尾部空列名
    while headers and headers[-1] == "":
        headers.pop()
    data: list[dict] = []
    for excel_row_idx, row in enumerate(rows_iter, start=2):
        if all(v is None or cell_str(v) == "" for v in row[: len(headers)]):
            continue
        item = {"_excel_row": excel_row_idx}
        for i, name in enumerate(headers):
            if not name:
                continue
            item[name] = cell_str(row[i]) if i < len(row) else ""
        data.append(item)
    wb.close()
    return headers, data


def is_dirty_background(bg: str) -> bool:
    if bg in BG_TRIVIAL:
        return True
    for tok in BG_NOISE_TOKENS:
        if tok in bg:
            return True
    return False


def quality_flags(row: dict) -> list[str]:
    flags: list[str] = []
    bg = row.get("background", "")
    hwp = row.get("current_hwp_message", "")
    history = row.get("history_messages", "")
    name = row.get("customer_name", "")
    title = row.get("title", "")
    reply = row.get("生产回复", "")

    if is_dirty_background(bg):
        flags.append("脏_background")
    if len(hwp) <= 2:
        flags.append("脏_hwp_过短")
    if not history or not hwp or not name or not title:
        flags.append("缺关键输入")
    if not reply:
        flags.append("生产回复为空")
    return flags


def annotate(rows: list[dict]) -> None:
    for row in rows:
        flags = quality_flags(row)
        dirty = [f for f in flags if f in DIRTY_RULES]
        row["_flags"] = flags
        row["_dirty_flags"] = dirty
        row["质量标记"] = "；".join(flags)
        row["样本分层"] = "脏样本" if dirty else "主集"


def stratified_sample(
    clean_rows: list[dict],
    per_instance: int,
    seed: int,
) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in clean_rows:
        key = (row.get("title", ""), row.get("instance_id", ""))
        groups[key].append(row)

    rng = random.Random(seed)
    sampled: list[dict] = []
    # 稳定遍历顺序，保证可复现
    for key in sorted(groups.keys(), key=lambda k: (k[0], _sort_id(k[1]))):
        bucket = list(groups[key])
        bucket.sort(key=lambda r: r["_excel_row"])
        if len(bucket) <= per_instance:
            chosen = bucket
        else:
            chosen = rng.sample(bucket, per_instance)
            chosen.sort(key=lambda r: r["_excel_row"])
        sampled.extend(chosen)
    return sampled


def _sort_id(sid: str):
    try:
        return (0, int(sid))
    except ValueError:
        return (1, sid)


def _rule_hits(rows: list[dict]) -> Counter:
    c: Counter = Counter()
    for row in rows:
        for f in row["_flags"]:
            c[f] += 1
    return c


def write_output(
    out_path: Path,
    headers: list[str],
    all_rows: list[dict],
    sampled: list[dict],
    include_dirty: bool,
    per_instance: int,
    seed: int,
) -> None:
    clean = [r for r in all_rows if r["样本分层"] == "主集"]
    dirty = [r for r in all_rows if r["样本分层"] == "脏样本"]
    table_rows = list(sampled)
    if include_dirty:
        # 脏样本全量附在主抽样之后，保持原行号序
        extra = sorted(dirty, key=lambda r: r["_excel_row"])
        table_rows.extend(extra)

    out_headers = list(headers)
    for col in META_COLS:
        if col not in out_headers:
            out_headers.append(col)

    wb = Workbook()

    # ---- 数据大表 ----
    ws_data = wb.active
    ws_data.title = "数据大表"
    ws_data.append(out_headers)
    for row in table_rows:
        ws_data.append([row.get(h, "") for h in out_headers])

    # ---- 质量说明 ----
    ws_q = wb.create_sheet("质量说明")
    ws_q.append(["规则汇总"])
    ws_q.append(["规则名", "命中行数", "是否默认剔除出主集", "说明"])
    hits = _rule_hits(all_rows)
    rule_notes = {
        "脏_background": "background 为空 / 「无」 / 含噪声词（如退款）",
        "脏_hwp_过短": "current_hwp_message 去空白后长度 ≤ 2",
        "缺关键输入": "history_messages / current_hwp_message / customer_name / title 任一为空",
        "生产回复为空": "初版输入集预期，不因此剔除",
    }
    for rule in list(DIRTY_RULES) + list(INFO_RULES):
        ws_q.append([
            rule,
            hits.get(rule, 0),
            "是" if rule in DIRTY_RULES else "否",
            rule_notes.get(rule, ""),
        ])

    ws_q.append([])
    ws_q.append(["说明项"])
    ws_q.append([
        "违规类型全空、生产回复全空属初版预期；"
        "边界 case 需后续人工补（本脚本不造数）；"
        f"抽样参数 per_instance={per_instance} seed={seed} include_dirty={include_dirty}"
    ])

    ws_q.append([])
    ws_q.append(["问题行清单（仅脏样本；「生产回复为空」见上方规则汇总）"])
    ws_q.append([
        "原表行号", "instance_id", "title", "样本分层", "规则",
        "background摘要", "HWP摘要",
    ])
    for row in sorted(dirty, key=lambda r: r["_excel_row"]):
        bg = row.get("background", "")
        hwp = row.get("current_hwp_message", "")
        ws_q.append([
            row["_excel_row"],
            row.get("instance_id", ""),
            row.get("title", ""),
            row["样本分层"],
            "；".join(row["_flags"]),
            bg[:40],
            hwp[:40],
        ])

    # ---- 行数统计 ----
    ws_s = wb.create_sheet("行数统计")
    n_all = len(all_rows)
    n_clean = len(clean)
    n_dirty = len(dirty)
    n_out = len(table_rows)
    n_bg_empty = sum(1 for r in all_rows if not r.get("background"))
    dirty_hit = sum(1 for r in all_rows if r["_dirty_flags"])

    ws_s.append(["总览"])
    ws_s.append(["指标", "数量"])
    for label, val in [
        ("全量行数", n_all),
        ("主集候选", n_clean),
        ("脏样本", n_dirty),
        ("最终写入数据大表", n_out),
        ("其中主集抽样", len(sampled)),
        ("其中附带脏样本", n_out - len(sampled) if include_dirty else 0),
        ("background 空值行", n_bg_empty),
        ("background 空值率", f"{n_bg_empty / n_all:.1%}" if n_all else "0%"),
        ("脏规则命中行", dirty_hit),
        ("脏规则命中率", f"{dirty_hit / n_all:.1%}" if n_all else "0%"),
        ("per_instance", per_instance),
        ("seed", seed),
    ]:
        ws_s.append([label, val])

    def layer_key(r: dict) -> tuple[str, str]:
        return (r.get("title", ""), r.get("instance_id", ""))

    clean_layers = {layer_key(r) for r in clean}
    sampled_layers = {layer_key(r) for r in sampled}
    missing_layers = sorted(clean_layers - sampled_layers)

    ws_s.append([])
    ws_s.append(["层覆盖（主集 title × instance_id）"])
    ws_s.append(["抽样前主集层数", len(clean_layers)])
    ws_s.append(["抽样后覆盖层数", len(sampled_layers)])
    ws_s.append(["未覆盖层数", len(missing_layers)])
    if missing_layers:
        ws_s.append(["未覆盖层清单（不应出现，除非该层主集为空）"])
        for t, iid in missing_layers:
            ws_s.append([t, iid])

    ws_s.append([])
    ws_s.append(["按 title（全量 / 主集候选 / 抽样）"])
    ws_s.append(["title", "全量", "主集候选", "抽样"])
    titles = sorted({r.get("title", "") for r in all_rows})
    for t in titles:
        ws_s.append([
            t,
            sum(1 for r in all_rows if r.get("title") == t),
            sum(1 for r in clean if r.get("title") == t),
            sum(1 for r in sampled if r.get("title") == t),
        ])

    ws_s.append([])
    ws_s.append(["按 instance_id（全量 / 主集候选 / 抽样）"])
    ws_s.append(["instance_id", "title", "全量", "主集候选", "抽样"])
    # 一个 instance 可能只挂在一个 title 下；按出现过的 (title,id) 列
    pairs = sorted(
        {(r.get("title", ""), r.get("instance_id", "")) for r in all_rows},
        key=lambda p: (p[0], _sort_id(p[1])),
    )
    for t, iid in pairs:
        ws_s.append([
            iid,
            t,
            sum(1 for r in all_rows if r.get("title") == t and r.get("instance_id") == iid),
            sum(1 for r in clean if r.get("title") == t and r.get("instance_id") == iid),
            sum(1 for r in sampled if r.get("title") == t and r.get("instance_id") == iid),
        ])

    wb.save(out_path)
    wb.close()


def print_summary(
    all_rows: list[dict],
    sampled: list[dict],
    include_dirty: bool,
    out_path: Path,
) -> None:
    clean = [r for r in all_rows if r["样本分层"] == "主集"]
    dirty = [r for r in all_rows if r["样本分层"] == "脏样本"]
    hits = _rule_hits(all_rows)
    clean_layers = {(r.get("title", ""), r.get("instance_id", "")) for r in clean}
    sampled_layers = {(r.get("title", ""), r.get("instance_id", "")) for r in sampled}
    n_out = len(sampled) + (len(dirty) if include_dirty else 0)

    print("==== 抽样完成 ====")
    print(f"全量: {len(all_rows)}  主集候选: {len(clean)}  脏样本: {len(dirty)}")
    print(f"主集抽样: {len(sampled)}  写入数据大表: {n_out}")
    print(f"层覆盖: {len(sampled_layers)}/{len(clean_layers)}")
    print("规则命中:", dict(hits))
    print(f"输出: {out_path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="面谈回复测评集分层抽样 → 三 sheet xlsx")
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="初版测评集路径")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出 xlsx 路径")
    p.add_argument("--per-instance", type=int, default=8, help="每层最多抽样条数")
    p.add_argument("--seed", type=int, default=42, help="随机种子")
    p.add_argument(
        "--include-dirty",
        action="store_true",
        help="脏样本也写入数据大表（样本分层=脏样本）",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.input.exists():
        raise SystemExit(f"输入不存在: {args.input}")
    if args.per_instance < 1:
        raise SystemExit("--per-instance 必须 ≥ 1")

    headers, rows = load_source(args.input)
    if not rows:
        raise SystemExit("输入无数据行")

    annotate(rows)
    clean = [r for r in rows if r["样本分层"] == "主集"]
    if not clean:
        raise SystemExit("主集候选为空，无法抽样（请检查脏样本规则）")

    sampled = stratified_sample(clean, args.per_instance, args.seed)

    # 验收：主集非空层必须至少抽到 1 条
    clean_layers = {(r.get("title", ""), r.get("instance_id", "")) for r in clean}
    sampled_layers = {(r.get("title", ""), r.get("instance_id", "")) for r in sampled}
    missing = clean_layers - sampled_layers
    if missing:
        raise SystemExit(f"层覆盖失败，未抽到: {sorted(missing)}")

    titles_out = {r.get("title", "") for r in sampled}
    if "接触面谈" not in titles_out or "成交面谈" not in titles_out:
        print(f"警告: 抽样 title 覆盖不全: {titles_out}")

    write_output(
        args.output,
        headers,
        rows,
        sampled,
        args.include_dirty,
        args.per_instance,
        args.seed,
    )
    print_summary(rows, sampled, args.include_dirty, args.output)


if __name__ == "__main__":
    main()
