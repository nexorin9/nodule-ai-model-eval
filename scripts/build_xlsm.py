#!/usr/bin/env python3
"""生成肺结节 AI 评估表模板。

用法::

    python scripts/build_xlsm.py                       # 生成 templates/肺结节AI评估表.xlsx
    python scripts/build_xlsm.py --out /path/to/表.xlsx --rows 300

**模板里有什么**：

- 「评估表」页（产品的界面本体，四个区块自上而下）：
  1. **表头汇总区** —— 样本量 / TP·FP·FN·TN / 敏感度·特异度·PPV·NPV /
     失败数 / 规则降级数 / 挂起数 / 不可比数；单元格位置固定，引擎按地址回填；
  2. **逐例明细区** —— 既有三列（检查号 / 金标准原文 / 影像引用，**科室维护**）
     + 产品新增六列（判定 / 模型返回 / 失败原因 / 调用记录ID / 待人工复核 /
     归并来源，**引擎回填**）。既有列一列都不改、也不挪位；两层用一行分组表头
     + 底色区分（原始件 / 展示件）；
  3. **漏诊明细区** —— 引擎回填，条数须与漏判数 FN 相等；
  4. **底部状态栏** —— 运行状态 / 最近更新 / 调用底账路径。
- 「字段说明」页：每列的中文名、由谁填、怎么填。

**模板本身不含宏**：``openpyxl`` 写不出 VBA 工程，宏以源码交付
（``src/nodule_eval/sheets/xlsm_vba/nodule_eval.bas`` 与
``src/nodule_eval/sheets/wps_jsa/nodule_eval.js``），安装步骤见
``docs/macro_install.md``。没有宏也能用：CSV 直跑 + ``fill`` 子命令是无宏退路。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nodule_eval.sheets.columns import ALL_COLUMNS, EXISTING_COLUMNS, NEW_COLUMNS  # noqa: E402
from nodule_eval.sheets.template import PREFILLED_ROWS, write_template  # noqa: E402

#: 模板缺省落盘目录（相对项目根）。
DEFAULT_OUT_DIR = Path("templates")

#: 模板缺省文件名。
DEFAULT_FILENAME = "肺结节AI评估表.xlsx"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成肺结节 AI 评估表模板")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR / DEFAULT_FILENAME),
        metavar="FILE",
        help=f"输出路径（缺省 {DEFAULT_OUT_DIR / DEFAULT_FILENAME}）",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=PREFILLED_ROWS,
        help=f"预置的空白数据行数（缺省 {PREFILLED_ROWS}）",
    )
    args = parser.parse_args(argv)

    target = write_template(args.out, prefilled_rows=args.rows)

    print(f"评估表模板已生成：{target}")
    print(f"既有列（科室维护）：{' / '.join(EXISTING_COLUMNS)}")
    print(f"新增列（引擎回填）：{' / '.join(NEW_COLUMNS)}")
    print(f"共 {len(ALL_COLUMNS)} 列，预置 {args.rows} 行空白。")
    print(
        "四个区块：表头汇总区（引擎回填）/ 逐例明细区 / 漏诊明细区 / 底部状态栏；"
        "汇总区单元格地址见 nodule_eval.sheets.template.SUMMARY_CELLS。"
    )
    print("宏的安装见 docs/macro_install.md；没有宏可用 CSV 直跑 + fill 子命令。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
