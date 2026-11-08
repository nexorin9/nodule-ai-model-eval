"""评估结论报告：漏诊清单 + run 级结论 + 手算可复核。

**这一层管什么**：把逐例判定折成**科室能带走的两份产物** ——
一份是**漏诊清单**（放射科逐例回看原调用），一份是**run 级结论**
（招采 / 上线 / 换版说明直接引用）。

判定（任务 26–28）与指标（任务 29）各管一件事，**报告层管的是
「这份结论能不能被人复核」**：

- **漏诊清单条数 ≡ 漏诊数 FN** —— 清单里数一数，与汇总行里的 FN 必须相等。
  两者对不上，说明有人把「不可比的例」也算进了漏诊 —— 那会把敏感度算低。
- **summary.json 的指标可反算** —— 落盘时把 TP / FP / FN / TN 四个原始计数
  与四个算式一并写进去（``manual_check.formulas``），
  复核的人拿计算器按 ``TP/(TP+FN)`` 逐项对一遍即可，不必信引擎。
- **对不上就报错** —— :func:`assert_summary_consistent` 在落盘前跑一遍，
  不一致时抛 ``CONTRACT_INVALID`` 并**点名差在哪一项**。

**一条红线**：**产物里不落金标准原文**。
原文可能带姓名 / 年龄 / 住院号；漏诊清单里的「金标准摘要」由
**归并后的结构化标签**（部位 / 尺寸 / 密度）拼成 ——
放射科看得懂，又不带患者可识别信息。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import (
    CaseRow,
    Metrics,
    MissedItem,
    Summary,
    VERDICT_MISS,
    VERDICTS,
    counts_toward_denominator,
    is_hold,
)
from ..errors import ErrorCode, NoduleEvalError
from ..error_matrix import failure_breakdown
from ..goldstd import GoldLabels
from .attributes import LAYER_MISMATCH
from .compute import (
    audit_metrics,
    count_model_normalized,
    count_raw_counts,
    count_rule_degraded,
    metric_values,
    summary_row_text,
)

#: 漏诊清单的列顺序（**与评估表新增列无关** —— 这是结论产物，不是回填列）。
#:
#: 定义在契约层（:mod:`nodule_eval.contracts`），产物写出与报告层共用同一份 ——
#: 两处各写一遍迟早写出两种列序，评审时对着表头看漏了哪一列都没人知道。
MISSED_LIST_COLUMNS: tuple[str, ...] = (
    "accession_no",
    "gold_standard_summary",
    "model_findings",
    "missed_reason",
    "tool_call_id",
    "rule_degraded",
)

#: 判定依据里四层的固定次序（与 ``Judgement.basis_text()`` 的生成顺序一致）。
_LAYER_ORDER: tuple[str, ...] = ("存在性", "部位", "尺寸", "密度")


# --------------------------------------------------------------------------
# 金标准摘要
# --------------------------------------------------------------------------


def lesion_summary_text(lesion: Any) -> str:
    """一处病灶的摘要 —— 部位 / 尺寸 / 密度（**认不出的属性不写**）。

    摘要取自**归并后的结构化标签**，不是原文摘抄：
    原文可能带姓名 / 年龄 / 住院号，产物里不落。
    """

    bits: list[str] = []
    location = getattr(lesion, "location", None)
    size_mm = getattr(lesion, "size_mm", None)
    density = getattr(lesion, "density", None)
    if location:
        bits.append(str(location))
    if isinstance(size_mm, (int, float)):
        bits.append(f"{float(size_mm):.1f}mm")
    if density:
        bits.append(str(density))
    if bits:
        return " ".join(bits)
    finding = str(getattr(lesion, "finding", "") or "")
    return finding or "金标准写明有病灶（未写部位 / 尺寸 / 密度）"


def gold_summary_text(gold: GoldLabels | None) -> str:
    """金标准摘要 —— 逐处病灶拼成的一句可读文本。

    供漏诊清单与逐例行携带「金标准说了什么」：
    放射科看清单时不用再去翻评估表；产物里又不带原文。
    """

    if gold is None:
        return ""
    parts: list[str] = []
    for lesion in getattr(gold, "lesions", ()) or ():
        parts.append(lesion_summary_text(lesion))
    if parts:
        return "；".join(parts)
    presence = getattr(gold, "presence", None)
    if presence is True:
        return "金标准写明有病灶（未写部位 / 尺寸 / 密度）"
    if presence is False:
        return "金标准写明未见结节"
    return "金标准的存在性读不出来（需人工复核）"


# --------------------------------------------------------------------------
# 漏诊原因
# --------------------------------------------------------------------------


def layer_conclusions(judge_basis: str) -> dict[str, str]:
    """从判定依据文本里取出四层结论（存在性 / 部位 / 尺寸 / 密度）。

    判定依据由 :meth:`nodule_eval.metrics.judge.Judgement.basis_text` 按固定
    格式生成（``存在性：一致；部位：一致；…``），这里按同一格式读回来 ——
    **只认这四个层名**，``覆盖`` 那一段单独处理（它带括号说明，不是结论值）。
    """

    out: dict[str, str] = {}
    for chunk in (judge_basis or "").split("；"):
        name, sep, value = chunk.partition("：")
        if not sep:
            continue
        name = name.strip()
        if name in _LAYER_ORDER:
            out[name] = value.strip()
    return out


def missed_reason_text(row: CaseRow) -> str:
    """漏判原因 —— 放射科直接看得懂的一段中文。

    三段话：**差在哪一层** + **没盖住的是哪一处** + **结论是什么**。
    不堆字段名（内部术语不直接往结论上贴）。
    """

    layers = layer_conclusions(row.judge_basis)
    mismatched = [name for name in _LAYER_ORDER if layers.get(name) == LAYER_MISMATCH]
    parts: list[str] = []

    if row.uncovered_labels:
        parts.append("金标准写了多处病灶，模型没有把其中一处对应上")
        parts.append(f"没盖住的是：{row.uncovered_labels}")
        if mismatched:
            parts.append(f"属性不一致的地方在「{'、'.join(mismatched)}」这一层")
        else:
            parts.append(
                "各属性层都能对上，差在「覆盖」这一层"
                "（金标准写了多处病灶，模型只盖住了其中一处）"
            )
    else:
        parts.append("金标准写明了结节，模型这次没有报出对应的病灶")
        parts.append("差在「存在性」这一层")

    return "；".join(parts)


# --------------------------------------------------------------------------
# 漏诊清单
# --------------------------------------------------------------------------


def build_missed_list(rows: Sequence[CaseRow]) -> tuple[MissedItem, ...]:
    """漏诊清单 —— **清单条数 ≡ 漏诊数 FN**。

    只收**计入指标分母**的漏诊例（``verdict == miss`` 且既没被挂起、
    也不可比）：清单条数与汇总行的 FN 必须相等 ——
    复核的人数一遍清单，拿到的漏诊数就能直接对上手算的敏感度。

    判为 ``miss`` 但**没进分母**的例（属性不可比）**不进清单**，
    它们在 ``summary.json`` 的 ``missed.not_counted`` 里单列 ——
    静默丢行才是最坏的写法。
    """

    items: list[MissedItem] = []
    for row in rows:
        if row.verdict != VERDICT_MISS:
            continue
        if not counts_toward_denominator(row):
            continue
        items.append(
            MissedItem(
                accession_no=row.accession_no,
                gold_standard_summary=row.gold_summary,
                model_findings=row.model_findings,
                missed_reason=missed_reason_text(row),
                tool_call_id=row.tool_call_id,
                rule_degraded=bool(row.rule_degraded),
            )
        )
    return tuple(items)


def missed_not_counted(rows: Sequence[CaseRow]) -> tuple[dict[str, str], ...]:
    """判为漏诊但**没进分母**的例 —— 单列出来，不静默丢。

    这些例的属性写法认不出来（不可比），没法核对到底漏没漏 ——
    它们不进敏感度分母，也不进漏诊清单，但**必须让人看见**：
    「这一例判了漏诊却没计入」不写出来，复核的人会以为清单漏了行。
    """

    out: list[dict[str, str]] = []
    for row in rows:
        if row.verdict != VERDICT_MISS:
            continue
        if counts_toward_denominator(row):
            continue
        if row.incomparable:
            reason = row.incomparable_reason or "属性不可比（写法认不出来），没法核对"
        elif is_hold(row):
            reason = "这一例挂起待复核，结论还没定"
        else:
            reason = "这一例未计入指标分母"
        out.append({"accession_no": row.accession_no, "reason": reason})
    return tuple(out)


def verdict_distribution(rows: Sequence[CaseRow]) -> dict[str, int]:
    """五种判定各多少例 —— **按逐例行的判定值统计**（与表上「判定」列一致）。

    注意它与 ``counts`` 的差别：这里统计的是**判定值**的分布，
    挂起例与不可比例的判定值也照数（表上就是这么写的）；
    真正进了 TP / FP / FN / TN 的是哪些，看 ``counts``。
    """

    counts = {verdict: 0 for verdict in VERDICTS}
    for row in rows:
        if row.verdict in counts:
            counts[row.verdict] += 1
    return counts


# --------------------------------------------------------------------------
# 手算复核
# --------------------------------------------------------------------------


def manual_check_block(rows: Sequence[CaseRow], metrics: Metrics) -> dict[str, Any]:
    """手算复核块 —— 落进 ``summary.json``，让人拿计算器就能对一遍。

    给出四样东西：

    - ``declared`` —— 汇总行里写着的那份原始计数；
    - ``recomputed`` —— 从逐例明细**反算**出来的那份；
    - ``formulas`` —— 四个指标各自的算式（连中间值都写出来）；
    - ``differences`` —— 对不上时逐项点名（一致时为空数组）。
    """

    audit = audit_metrics(rows, metrics)
    formulas: dict[str, str] = {}
    for value in metric_values(audit.recomputed):
        if value.value is None:
            formulas[value.name] = (
                f"{value.formula} = {value.numerator}/0 = null（{value.reason}）"
            )
        else:
            formulas[value.name] = (
                f"{value.formula} = {value.numerator}/{value.denominator} = {value.value:.4f}"
            )
    return {
        "consistent": audit.consistent,
        "declared": audit.declared.to_dict(),
        "recomputed": audit.recomputed.to_dict(),
        "formulas": formulas,
        "differences": list(audit.differences),
    }


def assert_summary_consistent(
    rows: Sequence[CaseRow],
    metrics: Metrics,
    summary: Summary,
    missed: Sequence[MissedItem],
) -> dict[str, Any]:
    """落盘前的一致性自检 —— **对不上就报错**，不给一份看着正常的结论。

    核四件事：

    1. **汇总指标 ↔ 逐例明细** —— 反算回 TP / FP / FN / TN 逐项核对
       （含「算不出（``null``）」这一态）；
    2. **汇总行的单列栏 ↔ 逐例明细** —— 失败数 / 挂起数 / 不可比数 /
       降级数 / 模型归并数；
    3. **漏诊清单条数 ≡ 漏诊数 FN** —— 数一遍清单就能对上敏感度；
    4. **样本量** —— 汇总行写的病例数与逐例行条数一致。

    任何一项对不上，抛 ``CONTRACT_INVALID`` 并**点名差在哪一项**。
    """

    differences: list[str] = []
    audit = audit_metrics(rows, metrics)
    differences.extend(audit.differences)

    counts = count_raw_counts(rows)
    checks = (
        ("进分母例数", summary.evaluated_cases, counts.counted),
        ("失败数", summary.failed_count, counts.failed),
        ("挂起数", summary.needs_review_count, counts.needs_review),
        ("不可比数", summary.incomparable_count, counts.incomparable),
        ("规则降级数", summary.rule_degraded_count, count_rule_degraded(rows)),
        ("模型归并数", summary.model_normalized_count, count_model_normalized(rows)),
        ("漏诊清单条数", len(missed), metrics.fn),
        ("病例数", summary.total_cases, len(rows)),
    )
    for label, said, got in checks:
        if said != got:
            differences.append(f"{label}对不上：汇总行写 {said}，逐例行数出 {got}")

    if differences:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail="评估结论与逐例明细对不上：" + "；".join(differences),
        )

    return manual_check_block(rows, metrics)


# --------------------------------------------------------------------------
# run 级结论
# --------------------------------------------------------------------------


def build_summary_payload(
    *,
    run_id: str,
    summary: Summary,
    metrics: Metrics,
    rows: Sequence[CaseRow],
    missed: Sequence[MissedItem],
    missed_list_path: str = "",
) -> dict[str, Any]:
    """run 级结论 —— ``runs/<run_id>/summary.json`` 的内容。

    招采 / 上线 / 换版说明要引用的东西都在这里：
    模型与版本、数据集引用、样本量、四项指标、失败数、降级数、挂起数、
    生成时间，外加**原始计数**与**手算复核块** ——
    光给百分比没法核对，给了百分比又给不了算式同样没法核对。
    """

    counts = count_raw_counts(rows)
    return {
        "run_id": run_id,
        "model_id": summary.model_id,
        "model_version": summary.model_version,
        "dataset_ref": summary.dataset_ref,
        "generated_at": summary.generated_at,
        "version_no": summary.version_no,
        "skipped_count": summary.skipped_count,
        "total_cases": summary.total_cases,
        "evaluated_cases": summary.evaluated_cases,
        "counts": {
            **counts.to_dict(),
            "rule_degraded": summary.rule_degraded_count,
            "model_normalized": summary.model_normalized_count,
        },
        "metrics": {
            "sensitivity": metrics.sensitivity,
            "sensitivity_reason": metrics.sensitivity_reason,
            "specificity": metrics.specificity,
            "specificity_reason": metrics.specificity_reason,
            "ppv": metrics.ppv,
            "ppv_reason": metrics.ppv_reason,
            "npv": metrics.npv,
            "npv_reason": metrics.npv_reason,
        },
        "summary_text": summary_row_text(metrics),
        "verdict_distribution": verdict_distribution(rows),
        # **按错误码汇总的失败分布**（任务 42）——
        # 与终端输出的「失败分布」、表格的 ``failure_reason`` 列同源：
        # 同一错误码三处是同一句话（文案只从 errors.py 取）。
        "failures": failure_breakdown(rows),
        "missed": {
            "count": len(missed),
            "fn": metrics.fn,
            "list_path": missed_list_path,
            "not_counted": list(missed_not_counted(rows)),
        },
        "degrade_note": summary.degrade_note,
        "manual_check": manual_check_block(rows, metrics),
    }


# --------------------------------------------------------------------------
# 产物写出
# --------------------------------------------------------------------------


def write_missed_list_csv(path: Path | str, items: Sequence[MissedItem]) -> Path:
    """写漏诊清单 CSV。

    列顺序取 :data:`~nodule_eval.contracts.MISSED_LIST_COLUMNS`；
    编码用 ``utf-8-sig`` —— 科室用 Excel 直接双击打开不乱码。
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MISSED_LIST_COLUMNS))
        writer.writeheader()
        for item in items:
            payload = item.to_dict()
            writer.writerow({key: payload.get(key, "") for key in MISSED_LIST_COLUMNS})
    return target


def read_missed_list_csv(path: Path | str) -> tuple[dict[str, Any], ...]:
    """读回漏诊清单 CSV（表头缺列即报契约错，不静默补空）。"""

    source = Path(path)
    if not source.is_file():
        return ()
    with open(source, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return ()
        missing = [name for name in MISSED_LIST_COLUMNS if name not in reader.fieldnames]
        if missing:
            raise NoduleEvalError(
                ErrorCode.CONTRACT_INVALID,
                detail=f"漏诊清单 CSV 缺列：{'、'.join(missing)}（文件 {source}）",
            )
        return tuple(dict(row) for row in reader)


def summary_payload_digest(payload: Mapping[str, Any]) -> str:
    """结论摘要的指纹 —— 同一轮结论两次落盘应当一致（供事后比对）。"""

    import hashlib
    import json

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
