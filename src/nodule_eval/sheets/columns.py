"""评估表的**列契约** —— 既有列与产品新增列的唯一真源。

**为什么单独放一个模块**：模板生成（任务 4 / 44）、逐例回填（任务 46）、
导出结论表（任务 39）三处都要按同一份列名与顺序操作工作簿。
三处各写一份字面量，改一次列名就会漏改一处 —— 回填写错列、
导出少一列、模板多一列，都是「看起来跑通、实际列错位」的静默事故。

**既有列由科室维护，产品只新增列，绝不覆盖既有列**：

- 既有列（3 列）：``accession_no`` / ``gold_standard_text`` / ``image_ref``
  —— 放射科自己维护，输入侧；``gold_standard_text`` 是金标准原文，**永不被改写**。
- 新增列（6 列）：判定与解释的**展示件**，由引擎回填：
  ``verdict`` / ``model_findings`` / ``failure_reason`` / ``tool_call_id`` /
  ``needs_review`` / ``normalize_source``。

新增六列的顺序与 :data:`nodule_eval.contracts.CASE_ROW_COLUMNS` 去掉 ``row_no`` 后一致
（逐例明细 CSV 与评估表新增列逐字对齐）—— 由 ``tests/test_template_columns.py`` 断言。
"""

from __future__ import annotations

from ..contracts import CASE_ROW_COLUMNS

#: 既有列（科室维护，输入侧）。顺序即工作簿里的列顺序。
EXISTING_COLUMNS: tuple[str, ...] = (
    "accession_no",
    "gold_standard_text",
    "image_ref",
)

#: 产品新增列（引擎回填，展示件）。顺序固定，**恰好六列**。
NEW_COLUMNS: tuple[str, ...] = (
    "verdict",
    "model_findings",
    "failure_reason",
    "tool_call_id",
    "needs_review",
    "normalize_source",
)

#: 工作簿的完整列顺序：既有列在前，新增列在后（不交错）。
ALL_COLUMNS: tuple[str, ...] = EXISTING_COLUMNS + NEW_COLUMNS

#: 新增列里**必填**（引擎每次回填都写值）的列 —— 其余列允许留空。
REQUIRED_NEW_COLUMNS: tuple[str, ...] = ("verdict", "tool_call_id")

#: 中文显示名 —— 供「字段说明」页与文档使用。
#: **表头单元格本身仍用契约列名**：宏与 ``openpyxl`` 回填都按契约列名定位，
#: 用中文表头会让定位规则多一套映射，两边一漂移就写错列。
COLUMN_LABELS_ZH: dict[str, str] = {
    "accession_no": "检查号",
    "gold_standard_text": "金标准原文",
    "image_ref": "影像引用",
    "verdict": "判定",
    "model_findings": "模型返回",
    "failure_reason": "失败原因",
    "tool_call_id": "调用记录ID",
    "needs_review": "待人工复核",
    "normalize_source": "归并来源",
}

#: 「字段说明」页的填写指引 —— 放射科与信息科看的部分。
COLUMN_HINTS_ZH: dict[str, str] = {
    "accession_no": "必填。RIS 的业务检查号，与影像清单一一对应。",
    "gold_standard_text": "必填。放射科写的金标准原文，自由文本可多行；产品全程只读不改。",
    "image_ref": "选填。留空时由引擎按检查号从 RIS 取；一行只放一个影像引用。",
    "verdict": "引擎回填。命中 / 漏判 / 误报 / 真阴性 / 失败 五值之一。",
    "model_findings": "引擎回填。被评模型这一例的返回摘要（部位 / 尺寸 / 密度）。",
    "failure_reason": "引擎回填。失败原因与错误码；正常例留空。",
    "tool_call_id": "引擎回填。本次调用的唯一编号，可据此反查入参与返回。",
    "needs_review": "引擎回填。是 / 否。规则降级或歧义挂起的例标「是」，等人工看。",
    "normalize_source": "引擎回填。model（模型归并）/ rule（规则降级）。",
}

#: 「字段说明」页的工作表名。
SHEET_COLUMNS = "字段说明"


def column_index(name: str) -> int:
    """取列在工作簿中的 1 基列号（Excel 的 A=1）。"""

    try:
        return ALL_COLUMNS.index(name) + 1
    except ValueError:
        raise KeyError(f"评估表里没有这一列：{name}") from None


def assert_alignment_with_case_rows() -> None:
    """断言新增列与逐例明细 CSV 的列一致（模板与底账不能各写一份）。

    逐例明细 CSV 是**独立产物**（``case_rows.csv``），列序是
    「检查号 + 新增六列 + 行号」；评估表是「既有三列 + 新增六列」。
    两边共有的部分就是**新增六列** —— 这一段必须逐字同序，
    否则从 CSV 回读再写进表时会整列错位。

    由 ``tests/test_template_columns.py`` 调用。列顺序漂移会让回填把
    「失败原因」写进「模型返回」，而两边的单测各自都能通过 —— 只有这一条能拦住。
    """

    csv_new_columns = tuple(
        key for key in CASE_ROW_COLUMNS if key not in EXISTING_COLUMNS and key != "row_no"
    )
    if csv_new_columns != NEW_COLUMNS:
        raise AssertionError(
            f"新增列与逐例明细 CSV 不一致：CSV {csv_new_columns} vs 评估表 {NEW_COLUMNS}"
        )
