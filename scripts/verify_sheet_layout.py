#!/usr/bin/env python3
"""评估表模板**版面**的实跑核对（任务 44）。

**不是**「文件能打开」的检查 —— 真生成一份评估表，把四个区块的位置、
汇总区的固定单元格地址、分组表头的归属与底色、冻结与打印设置
逐条读回来核对并打印。

用法::

    python scripts/verify_sheet_layout.py                # 生成到临时目录
    python scripts/verify_sheet_layout.py --out 评估表.xlsx --rows 200
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openpyxl import load_workbook  # noqa: E402

from nodule_eval.sheets.columns import ALL_COLUMNS, EXISTING_COLUMNS  # noqa: E402
from nodule_eval.sheets.sections import is_section_marker  # noqa: E402
from nodule_eval.sheets.template import (  # noqa: E402
    SUMMARY_CELLS,
    SUMMARY_LABELS,
    find_sections,
    layout_for,
    write_template,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对评估表模板版面")
    parser.add_argument("--out", default="", help="输出路径（缺省写到临时目录）")
    parser.add_argument("--rows", type=int, default=200, help="预置空白行数")
    args = parser.parse_args(argv)

    if args.out:
        target = Path(args.out)
    else:
        target = Path(tempfile.mkdtemp(prefix="nodule-layout-")) / "评估表.xlsx"

    write_template(target, prefilled_rows=args.rows)
    workbook = load_workbook(target)
    sheet = workbook[workbook.sheetnames[0]]
    layout = layout_for(args.rows)

    print(f"工作簿            : {target}")
    print(f"工作表            : {workbook.sheetnames}")
    print(f"冻结锚点          : {sheet.freeze_panes}（表头行 {layout.header_row}）")
    print(f"网格线            : {'关' if not sheet.sheet_view.showGridLines else '开'}")

    print("\n--- 四个区块 ---")
    print(f"① 表头汇总区      : 第 {layout.summary_section_row} 行小标题，"
          f"标签第 {layout.summary_label_rows} 行，数值第 {layout.summary_value_rows} 行")
    print(f"② 逐例明细区      : 分组表头第 {layout.group_header_row} 行，"
          f"列头第 {layout.header_row} 行，数据第 {layout.first_data_row}–{layout.last_data_row} 行")
    print(f"③ 漏诊明细区      : 第 {layout.missed_section_row} 行区块标题，"
          f"表头第 {layout.missed_header_row} 行，数据第 {layout.missed_first_data_row}–{layout.missed_last_data_row} 行")
    print(f"④ 底部状态栏      : 第 {layout.status_row} 行")

    print("\n--- 汇总区固定单元格（引擎回填按这张表写）---")
    for key, address in SUMMARY_CELLS.items():
        cell = sheet[address]
        print(f"  {key:<24} {address:<5} {SUMMARY_LABELS[key]:<12} "
              f"值={cell.value!r} 格式={cell.number_format}")

    print("\n--- 明细区分组（既有列 = 原始件 / 新增列 = 展示件）---")
    row = layout.group_header_row
    split = len(EXISTING_COLUMNS)
    existing = sheet.cell(row=row, column=1)
    new = sheet.cell(row=row, column=split + 1)
    print(f"  左组 A–{chr(64 + split)}：{existing.value}")
    print(f"         底色={existing.fill.patternType or '无（原始件保持白底）'}")
    print(f"  右组 {chr(65 + split)}–{chr(64 + len(ALL_COLUMNS))}：{new.value}")
    print(f"         底色={new.fill.patternType} {new.fill.start_color.rgb}")
    print(f"  列头顺序          ：{[sheet.cell(row=layout.header_row, column=i).value for i in range(1, len(ALL_COLUMNS) + 1)]}")

    print("\n--- 区块标记（读层见到即停）---")
    for name, line in find_sections(sheet).items():
        print(f"  {name:<20} 第 {line} 行：{sheet.cell(row=line, column=1).value}")
    print(f"  状态栏行是标记行  ：{is_section_marker(sheet.cell(row=layout.status_row, column=1).value)}")

    print("\n--- 打印设置 ---")
    print(f"  方向              ：{sheet.page_setup.orientation}")
    print(f"  缩放到一页宽      ：fitToWidth={sheet.page_setup.fitToWidth} "
          f"fitToHeight={sheet.page_setup.fitToHeight} "
          f"fitToPage={sheet.sheet_properties.pageSetUpPr.fitToPage}")
    print(f"  打印区域          ：{sheet.print_area}")
    print(f"  每页重复标题行    ：{sheet.print_title_rows}")
    print(f"  页边距            ：左 {sheet.page_margins.left} 右 {sheet.page_margins.right} "
          f"上 {sheet.page_margins.top} 下 {sheet.page_margins.bottom}")

    print("\n--- 汇总区标签（供科室对照）---")
    for row_no in layout.summary_label_rows:
        cells = [sheet.cell(row=row_no, column=i).value for i in range(1, len(ALL_COLUMNS) + 1)]
        print(f"  第 {row_no} 行：{cells}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
