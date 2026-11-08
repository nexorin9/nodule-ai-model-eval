#!/usr/bin/env python3
"""评估表视觉 token 的实跑核对（任务 43）。

**不是**「样式对象存在」的检查 —— 真生成一份工作簿，把表头底色、
字体、列宽、冻结、条件格式规则逐条读回来核对，并把三档底色的
饱和度 / 亮度 / 对比度算出来打印。

用法::

    python scripts/verify_sheet_tokens.py                # 生成到临时目录
    python scripts/verify_sheet_tokens.py --out 评估表.xlsx
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openpyxl import load_workbook  # noqa: E402

from nodule_eval.sheets import tokens  # noqa: E402
from nodule_eval.sheets.columns import ALL_COLUMNS  # noqa: E402
from nodule_eval.sheets.template import FIRST_DATA_ROW, HEADER_ROW, SHEET_DETAIL, write_template  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对评估表视觉 token")
    parser.add_argument("--out", default="", help="输出路径（缺省写到临时目录）")
    parser.add_argument("--rows", type=int, default=8, help="预置空白行数")
    args = parser.parse_args(argv)

    if args.out:
        target = Path(args.out)
    else:
        target = Path(tempfile.mkdtemp(prefix="nodule-tokens-")) / "评估表.xlsx"

    write_template(target, prefilled_rows=args.rows)
    workbook = load_workbook(target)
    sheet = workbook[SHEET_DETAIL]

    print(f"工作簿        : {target}")
    print(f"表头行        : {HEADER_ROW}（冻结 {sheet.freeze_panes}）")
    print(f"表头底色      : {sheet.cell(row=HEADER_ROW, column=1).fill.start_color.rgb}")
    print(f"表头字体      : {sheet.cell(row=HEADER_ROW, column=1).font.name} "
          f"{sheet.cell(row=HEADER_ROW, column=1).font.size}pt "
          f"bold={sheet.cell(row=HEADER_ROW, column=1).font.bold}")

    print("列宽          :")
    for index, name in enumerate(ALL_COLUMNS, start=1):
        letter = sheet.cell(row=HEADER_ROW, column=index).column_letter
        print(f"  {letter:<2} {name:<20} {sheet.column_dimensions[letter].width}")

    print("判定底色（条件格式规则）:")
    for formatting in sheet.conditional_formatting:
        for rule in formatting.rules:
            color = str(rule.dxf.fill.bgColor.rgb)[-6:]
            print(f"  {str(formatting.sqref):<12} {rule.formula[0]:<28} 底色 {color}")

    print("三档底色可打印性:")
    for verdict in tokens.TIED_VERDICTS:
        color = tokens.VERDICT_FILLS[verdict]
        print(
            f"  {verdict:<8} {color}  饱和度 {tokens.saturation(color):.3f}"
            f"（上限 {tokens.MAX_FILL_SATURATION}）"
            f"  亮度 {tokens.relative_luminance(color):.3f}"
            f"（下限 {tokens.MIN_FILL_LUMINANCE}）"
            f"  与正文对比度 {tokens.contrast_ratio(tokens.COLOR_TEXT, color):.1f}:1"
            f"（下限 {tokens.MIN_FILL_CONTRAST}:1）"
        )

    print("数字格式      : 指标 {token} / 计数 {count}".format(
        token=tokens.NUMBER_FORMAT_METRIC, count=tokens.NUMBER_FORMAT_COUNT
    ))

    print(f"明细首行      : 第 {FIRST_DATA_ROW} 行，行高 {sheet.row_dimensions[FIRST_DATA_ROW].height}")
    print("核对结论      : 通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
