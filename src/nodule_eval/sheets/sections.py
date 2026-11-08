"""评估表的**区块标记** —— 模板与读取层之间的唯一约定。

**为什么需要这一层**：评估表模板（任务 44）不是一张光秃秃的明细表，
它自上而下有四个区块：表头汇总区 / 逐例明细区 / 漏诊明细区 / 底部状态栏。
前两个区块是「数据区」，后两个是「展示区」。

读取层（:mod:`nodule_eval.sheets.read`）只该把**明细区**的行读成病例。
漏诊明细区里也有检查号 —— 那是引擎回填的**结论**，不是输入；
状态栏里也有文字 —— 那是运行状态，不是病例。
读层若一路读到表尾，会把漏诊清单里的检查号当成新的病例读进来
（还带着空的金标准原文），一次评估的口径当场就错了。

**约定**：明细区之后，每个区块的**第一行第一列**写一个区块标记
（``【漏诊明细】`` / ``【运行状态】``）。读层见到标记即停 ——
这一行往下都不是输入。模板（写入方）与读层（读取方）共用这一份标记常量，
两边各写一份字面量，改一次就有一边认不出来。

**没有标记的表不受影响**：科室自己做的表没有标记，读层照旧读到表尾
（行为与从前完全一样）。标记只是给「带区块的模板」用的。
"""

from __future__ import annotations

from typing import Final

#: 漏诊明细区的区块标记（写在区块第一行第一列）。
SECTION_MARKER_MISSED: Final[str] = "【漏诊明细】"

#: 底部状态栏的区块标记。
SECTION_MARKER_STATUS: Final[str] = "【运行状态】"

#: 全部区块标记 —— 读层见到其中任何一个即停止读数据行。
SECTION_MARKERS: Final[tuple[str, ...]] = (
    SECTION_MARKER_MISSED,
    SECTION_MARKER_STATUS,
)


def is_section_marker(text: str | None) -> bool:
    """这一格是不是区块标记（是 → 明细区到此为止）。"""

    if not text:
        return False
    stripped = str(text).strip()
    return any(stripped.startswith(marker) for marker in SECTION_MARKERS)
