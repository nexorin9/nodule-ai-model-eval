"""结论表导出：把一轮评估的结论导成科室能直接带走的文件。

**为什么要有这条出口**：表格内插件（xlsm 宏 / WPS JSA 宏）是本产品的
**首要入口**，但**不是每台机器都能装宏** —— 宏信任目录受组策略限制、
Excel 与 WPS 的宏 API 还不一样、放射科自己的电脑也未必装得了。
结论必须有一条**不依赖宏**的出路：

    nodule-eval export --run-id <run_id> --out-dir <目录>

**导出的内容全部取自 run store**（库里的逐例行是真源）——
不重新算一遍、也不从 CSV 再解析一遍。二次解析会把「产物被改过」这件事
藏起来：有人手改了 ``case_rows.csv``，导出也跟着变，而库里那份没变，
两边的结论就对不上了却没人知道。

**一条红线**：导出件里**不落金标准原文**。
原文可能带姓名 / 年龄 / 住院号 —— 结论表是要往外发的东西（招采、评审、
飞检说明）。逐例明细里的「金标准」一栏给的是**归并后的结构化标签**
（部位 / 尺寸 / 密度）拼成的摘要，与漏诊清单同一口径。

**产物三件套**：

- ``评估结论_<run_id>.xlsx`` —— 三页工作簿（评估结论 / 逐例明细 / 漏诊清单）
  + 一页字段说明；结论页带**手算复核算式**，拿计算器就能对一遍；
- ``漏诊清单_<run_id>.csv`` —— 与 ``runs/<run_id>/missed_list.csv`` 同源同口径
  （``utf-8-sig``，Excel 双击打开不乱码）；
- ``逐例明细_<run_id>.csv`` —— 与 ``runs/<run_id>/case_rows.csv`` 同源，
  给不用 Excel 的场合（导入统计、贴进邮件）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import CaseRow
from .errors import ErrorCode, NoduleEvalError
from .metrics.compute import (
    compute_metrics_from_rows,
    count_model_normalized,
    count_raw_counts,
    count_rule_degraded,
    metric_values,
)
from .metrics.report import (
    MISSED_LIST_COLUMNS,
    build_missed_list,
    manual_check_block,
    missed_not_counted,
    verdict_distribution,
)
from .sheets.columns import COLUMN_HINTS_ZH, COLUMN_LABELS_ZH

#: 导出逐例明细的列顺序（**结论产物的列**，不是评估表的列）。
#:
#: 与评估表新增六列的差别有两处，都是有意的：
#:
#: 1. **没有「金标准原文」列** —— 原文可能带患者可识别明细，产物不落；
#:    取而代之的是 ``gold_summary``（归并后的结构化标签摘要）；
#: 2. **多了「判定依据」列** —— 「这一例凭什么这么判」要一眼看得见，
#:    复核的人不翻底稿就能看懂。
EXPORT_CASE_COLUMNS: tuple[str, ...] = (
    "accession_no",
    "verdict",
    "model_findings",
    "gold_summary",
    "judge_basis",
    "failure_reason",
    "tool_call_id",
    "needs_review",
    "normalize_source",
)

#: 导出件里各列的中文名（表头仍是契约名，中文给「字段说明」页看）。
EXPORT_COLUMN_LABELS_ZH: dict[str, str] = {
    **COLUMN_LABELS_ZH,
    "gold_summary": "金标准摘要",
    "judge_basis": "判定依据",
}

#: 导出件里各列的说明文案。
EXPORT_COLUMN_HINTS_ZH: dict[str, str] = {
    **COLUMN_HINTS_ZH,
    "gold_summary": (
        "金标准归并后的结构化标签摘要（部位 / 尺寸 / 密度），不是金标准原文 —— "
        "原文可能带患者可识别信息，导出件里不落。"
    ),
    "judge_basis": "判定依据：存在性 / 部位 / 尺寸 / 密度四层结论 + 覆盖结论。",
}

#: 导出格式。
EXPORT_FORMAT_XLSX = "xlsx"
EXPORT_FORMAT_CSV = "csv"
EXPORT_FORMATS: tuple[str, ...] = (EXPORT_FORMAT_XLSX, EXPORT_FORMAT_CSV)

#: 工作簿里的工作表名。
SHEET_CONCLUSION = "评估结论"
SHEET_CASES = "逐例明细"
SHEET_MISSED = "漏诊清单"
SHEET_FIELDS = "字段说明"


@dataclass(frozen=True)
class ExportResult:
    """一次导出的结果 —— 每个产物文件落在哪、里面有多少行。"""

    run_id: str
    out_dir: str
    workbook_path: str = ""
    missed_list_path: str = ""
    case_rows_path: str = ""
    case_count: int = 0
    missed_count: int = 0
    formats: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "out_dir": self.out_dir,
            "workbook_path": self.workbook_path,
            "missed_list_path": self.missed_list_path,
            "case_rows_path": self.case_rows_path,
            "case_count": self.case_count,
            "missed_count": self.missed_count,
            "formats": list(self.formats),
        }


def _case_row_values(row: CaseRow) -> list[str]:
    """把一条逐例行摊成导出件的一行（按 :data:`EXPORT_CASE_COLUMNS`）。"""

    values: list[str] = []
    for name in EXPORT_CASE_COLUMNS:
        value = getattr(row, name, "")
        if isinstance(value, bool):
            values.append("是" if value else "否")
        else:
            values.append("" if value is None else str(value))
    return values


def build_conclusion_rows(
    *,
    run_id: str,
    model_id: str,
    model_version: str,
    dataset_ref: str,
    generated_at: str,
    rows: Sequence[CaseRow],
    summary_payload: Mapping[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """评估结论页的「项目 / 值」两列。

    **四项指标连算式一起给** —— 只给百分比没法核对，给了百分比又给不了
    算式同样没法核对。算式取自 :func:`manual_check_block`（与
    ``summary.json`` 同一份），复核的人拿计算器按 ``TP/(TP+FN)`` 对一遍即可。
    """

    metrics = compute_metrics_from_rows(rows)
    counts = count_raw_counts(rows)
    missed = build_missed_list(rows)
    manual = manual_check_block(rows, metrics)

    out: list[tuple[str, str]] = [
        ("run_id", run_id),
        ("被评模型", model_id),
        ("模型版本", model_version),
        ("数据集引用", dataset_ref),
        ("生成时间", generated_at),
    ]

    out.append(("—— 样本量 ——", ""))
    out.append(("病例数", str(len(rows))))
    out.append(("进分母例数", str(counts.counted)))
    out.append(("失败数", str(counts.failed)))
    out.append(("挂起数", str(counts.needs_review)))
    out.append(("不可比数", str(counts.incomparable)))
    out.append(("规则降级数", str(count_rule_degraded(rows))))
    out.append(("模型归并数", str(count_model_normalized(rows))))

    out.append(("—— 原始计数 ——", ""))
    out.append(("TP（命中）", str(metrics.tp)))
    out.append(("FP（误报）", str(metrics.fp)))
    out.append(("FN（漏判）", str(metrics.fn)))
    out.append(("TN（真阴性）", str(metrics.tn)))

    out.append(("—— 指标（含手算算式）——", ""))
    for value in metric_values(counts):
        if value.value is None:
            rendered = f"null（{value.reason}）"
        else:
            rendered = f"{value.value:.4f}"
        out.append(
            (
                value.label,
                f"{rendered}　算式：{value.formula} = {value.numerator}/{value.denominator}",
            )
        )

    out.append(("—— 结论核对 ——", ""))
    out.append(("漏诊清单条数", f"{len(missed)}（须与 FN 相等：{metrics.fn}）"))
    out.append(("漏判但未计入分母", str(len(missed_not_counted(rows)))))
    out.append(
        (
            "汇总与逐例明细核对",
            "一致" if manual["consistent"] else "不一致：" + "；".join(manual["differences"]),
        )
    )
    distribution = verdict_distribution(rows)
    out.append(
        (
            "判定分布",
            "、".join(f"{name} {distribution.get(name, 0)}" for name in distribution),
        )
    )
    if summary_payload is not None:
        note = summary_payload.get("degrade_note")
        if isinstance(note, str) and note.strip():
            out.append(("降级说明", note))

    return out


def _write_workbook(
    path: Path,
    *,
    conclusion: Sequence[tuple[str, str]],
    rows: Sequence[CaseRow],
    missed: Sequence[Mapping[str, Any]],
) -> Path:
    """写结论工作簿（三页数据 + 一页字段说明）。"""

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    header_fill = PatternFill(start_color="1F3A5F", end_color="1F3A5F", fill_type="solid")
    header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
    body_font = Font(name="Microsoft YaHei", size=10)
    section_fill = PatternFill(start_color="E8EEF5", end_color="E8EEF5", fill_type="solid")
    border = Border(bottom=Side(style="thin", color="D0D7E2"))

    workbook = Workbook()

    # ---- 评估结论 ----
    sheet = workbook.active
    sheet.title = SHEET_CONCLUSION
    sheet.cell(row=1, column=1, value="项目")
    sheet.cell(row=1, column=2, value="值")
    for index in (1, 2):
        cell = sheet.cell(row=1, column=index)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for offset, (label, value) in enumerate(conclusion):
        row = 2 + offset
        label_cell = sheet.cell(row=row, column=1, value=label)
        label_cell.font = body_font
        if value == "" and label.startswith("——"):
            label_cell.fill = section_fill
            label_cell.font = Font(name="Microsoft YaHei", size=10, bold=True)
        sheet.cell(row=row, column=2, value=value).font = body_font
        for index in (1, 2):
            sheet.cell(row=row, column=index).border = border
    sheet.column_dimensions[get_column_letter(1)].width = 22
    sheet.column_dimensions[get_column_letter(2)].width = 72
    sheet.freeze_panes = sheet["A2"]

    # ---- 逐例明细 ----
    cases_sheet = workbook.create_sheet(SHEET_CASES)
    for index, name in enumerate(EXPORT_CASE_COLUMNS, start=1):
        cell = cases_sheet.cell(row=1, column=index, value=name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cases_sheet.column_dimensions[get_column_letter(index)].width = (
            46 if name in ("model_findings", "gold_summary", "judge_basis", "failure_reason") else 18
        )
    for offset, row in enumerate(rows):
        for index, value in enumerate(_case_row_values(row), start=1):
            cell = cases_sheet.cell(row=2 + offset, column=index, value=value)
            cell.font = body_font
            cell.alignment = Alignment(vertical="center", wrap_text=False)
            cell.border = border
    cases_sheet.row_dimensions[1].height = 22
    cases_sheet.freeze_panes = cases_sheet.cell(row=2, column=1)

    # ---- 漏诊清单 ----
    missed_sheet = workbook.create_sheet(SHEET_MISSED)
    for index, name in enumerate(MISSED_LIST_COLUMNS, start=1):
        cell = missed_sheet.cell(row=1, column=index, value=name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        missed_sheet.column_dimensions[get_column_letter(index)].width = (
            46 if name in ("gold_standard_summary", "model_findings", "missed_reason") else 18
        )
    for offset, item in enumerate(missed):
        for index, name in enumerate(MISSED_LIST_COLUMNS, start=1):
            value = item.get(name, "")
            cell = missed_sheet.cell(
                row=2 + offset,
                column=index,
                value="" if value is None else str(value),
            )
            cell.font = body_font
            cell.border = border
    missed_sheet.row_dimensions[1].height = 22
    missed_sheet.freeze_panes = missed_sheet.cell(row=2, column=1)

    # ---- 字段说明 ----
    fields_sheet = workbook.create_sheet(SHEET_FIELDS)
    for index, title in enumerate(("列名（契约）", "中文名", "说明"), start=1):
        cell = fields_sheet.cell(row=1, column=index, value=title)
        cell.fill = section_fill
        cell.font = Font(name="Microsoft YaHei", size=10, bold=True)
    for offset, name in enumerate(EXPORT_CASE_COLUMNS):
        row = 2 + offset
        fields_sheet.cell(row=row, column=1, value=name).font = body_font
        fields_sheet.cell(row=row, column=2, value=EXPORT_COLUMN_LABELS_ZH.get(name, name)).font = body_font
        fields_sheet.cell(row=row, column=3, value=EXPORT_COLUMN_HINTS_ZH.get(name, "")).font = body_font
    for index, width in enumerate((24, 16, 78), start=1):
        fields_sheet.column_dimensions[get_column_letter(index)].width = width
    fields_sheet.freeze_panes = fields_sheet["A2"]

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> Path:
    """写 CSV（``utf-8-sig``，Excel 双击打开不乱码）。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(columns))
        for row in rows:
            writer.writerow(list(row))
    return path


def export_run(
    store: Any,
    run_id: str,
    out_dir: Path | str,
    *,
    formats: Sequence[str] = EXPORT_FORMATS,
) -> ExportResult:
    """把一轮评估的结论导出到 ``out_dir``。

    ``store``
        run store —— **导出的真源**。逐例行取自库里（``recompute``），
        不从 CSV 再解析。
    ``run_id``
        要导出哪一轮。库里没有这一轮 → ``CONTRACT_INVALID`` 并指名。
    ``out_dir``
        产物落点。缺省由调用方给（CLI 缺省 ``runs/<run_id>/export/``）。
    ``formats``
        要导哪些格式：``xlsx`` / ``csv`` / 两者都要。

    **产物里不落金标准原文**（见模块开头的红线说明）。
    """

    key = (run_id or "").strip()
    if not key:
        raise NoduleEvalError(ErrorCode.CONTRACT_INVALID, detail="run_id 不得为空")
    run = store.get_run(key)
    if run is None:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"运行库里没有这一轮：{key}",
        )

    wanted = tuple(formats) or EXPORT_FORMATS
    unknown = [name for name in wanted if name not in EXPORT_FORMATS]
    if unknown:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"导出格式只能是 {'、'.join(EXPORT_FORMATS)}（实得 {'、'.join(unknown)}）"
            ),
        )

    rows = store.recompute(key)
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)

    try:
        summary_payload = store.read_json_artifact(key, "summary.json")
    except (OSError, NoduleEvalError):
        summary_payload = None
    if not isinstance(summary_payload, Mapping):
        summary_payload = None

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    result = ExportResult(
        run_id=key,
        out_dir=str(target),
        case_count=len(rows),
        missed_count=len(missed),
        formats=wanted,
    )

    if EXPORT_FORMAT_XLSX in wanted:
        conclusion = build_conclusion_rows(
            run_id=key,
            model_id=run.model_id,
            model_version=run.model_version,
            dataset_ref=run.dataset_ref,
            generated_at=str(summary_payload.get("generated_at") or "") if summary_payload else "",
            rows=rows,
            summary_payload=summary_payload,
        )
        workbook_path = _write_workbook(
            target / f"评估结论_{key}.xlsx",
            conclusion=conclusion,
            rows=rows,
            missed=[item.to_dict() for item in missed],
        )
        result = ExportResult(
            run_id=result.run_id,
            out_dir=result.out_dir,
            workbook_path=str(workbook_path),
            case_count=result.case_count,
            missed_count=result.missed_count,
            formats=result.formats,
        )

    if EXPORT_FORMAT_CSV in wanted:
        missed_path = _write_csv(
            target / f"漏诊清单_{key}.csv",
            MISSED_LIST_COLUMNS,
            [
                [
                    "" if item.to_dict().get(name) is None else str(item.to_dict().get(name, ""))
                    for name in MISSED_LIST_COLUMNS
                ]
                for item in missed
            ],
        )
        cases_path = _write_csv(
            target / f"逐例明细_{key}.csv",
            EXPORT_CASE_COLUMNS,
            [_case_row_values(row) for row in rows],
        )
        result = ExportResult(
            run_id=result.run_id,
            out_dir=result.out_dir,
            workbook_path=result.workbook_path,
            missed_list_path=str(missed_path),
            case_rows_path=str(cases_path),
            case_count=result.case_count,
            missed_count=result.missed_count,
            formats=result.formats,
        )

    return result
