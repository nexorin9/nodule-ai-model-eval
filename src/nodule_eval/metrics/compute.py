"""指标计算：敏感度 / 特异度 / PPV / NPV + 原始计数 + null 语义。

**这一层管什么**：把逐例判定折成**四项指标**与**一份汇总行**，
并保证这份汇总行**能被人工手算逐项复核**。

**口径真源**：:mod:`nodule_eval.contracts` 的 ``counts_toward_denominator()`` ——
进不进指标分母**不在本模块另写一份**。失败例（``failed``）、挂起例
（``held``）与不可比例（``incomparable``）一律不进分母，各自单列计数。

**一条红线**：**分母为 0 时输出 ``null``，绝不输出 0**。
敏感度算不出与敏感度等于 0 是两件完全不同的事：
前者是「这一轮没有阳性样本，分母为 0」，后者是「阳性样本一个没命中」。
把前者写成 0，评审会上就会拿着一条假结论去否决一款模型 ——
所以分母为 0 时**必须写明原因**，让人知道这一栏是「算不出」而不是「算出来是 0」。

**取整口径**：四舍六入五入成双（``ROUND_HALF_EVEN``），保留四位小数；
原始计数保持整数 —— 复核的人拿计算器按 TP/FP/FN/TN 手算，一眼能对上。

**手算复核入口**：:func:`audit_metrics` / :func:`assert_metrics_consistent` ——
给定逐例行与汇总指标，**反算**回 TP / FP / FN / TN 并逐项核对，
不一致时点名差在哪一项。汇总行不是「引擎算完就信」的东西，是**可被复算**的。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Mapping, Sequence

from ..contracts import (
    CaseRow,
    Metrics,
    NORMALIZE_SOURCE_MODEL,
    VERDICT_FALSE_POSITIVE,
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_TRUE_NEGATIVE,
    counts_toward_denominator,
    is_hold,
)
from ..errors import ErrorCode, NoduleEvalError

#: 指标保留的小数位数（四位，够评审会用，也不至于多到看不出是手算的）。
METRIC_DECIMALS = 4

#: 四舍六入五入成双的量化单位（由 ``METRIC_DECIMALS`` 推出，不另写死）。
_QUANTUM = Decimal(1).scaleb(-METRIC_DECIMALS)

#: 四项指标的**空分母原因** —— 分母为 0 时写进 ``*_reason``，
#: 让人一眼看出「这一栏是算不出来的」，而不是「算出来是 0」。
_ZERO_DENOMINATOR_REASONS: Mapping[str, str] = {
    "sensitivity": "本轮没有金标准阳性的样本（TP+FN=0），分母为 0，敏感度算不出（不是 0）",
    "specificity": "本轮没有金标准阴性的样本（TN+FP=0），分母为 0，特异度算不出（不是 0）",
    "ppv": "本轮模型没有报出任何结节（TP+FP=0），分母为 0，阳性预测值算不出（不是 0）",
    "npv": "本轮模型没有判为阴性的样本（TN+FN=0），分母为 0，阴性预测值算不出（不是 0）",
}


# --------------------------------------------------------------------------
# 原始计数
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RawCounts:
    """四项原始计数 + 各栏单列的例数。

    ``counted``
        真正进了指标分母的例数 —— ``tp + fp + fn + tn`` 必须等于它。
    ``failed`` / ``needs_review`` / ``incomparable``
        被隔离在分母之外的三类例，**各占一栏、互不吞并**。
    """

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    counted: int = 0
    failed: int = 0
    needs_review: int = 0
    incomparable: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "counted": self.counted,
            "failed": self.failed,
            "needs_review": self.needs_review,
            "incomparable": self.incomparable,
        }


def count_raw_counts(rows: Sequence[CaseRow]) -> RawCounts:
    """数原始计数：**只有进了指标分母的例才进 TP / FP / FN / TN**。

    分母口径真源仍是 :func:`nodule_eval.contracts.counts_toward_denominator`
    —— 失败例、挂起例与不可比例一律不进分母，各自单列计数。
    这份口径与 ``contracts.partition_rows`` 同源，两处不各写一份。
    """

    counts = RawCounts()
    for row in rows:
        if is_hold(row):
            counts = _bump(counts, "needs_review")
        elif row.verdict == "failed":
            counts = _bump(counts, "failed")
        elif row.incomparable:
            counts = _bump(counts, "incomparable")
        if not counts_toward_denominator(row):
            continue
        counts = _bump(counts, "counted")
        if row.verdict == VERDICT_HIT:
            counts = _bump(counts, "tp")
        elif row.verdict == VERDICT_MISS:
            counts = _bump(counts, "fn")
        elif row.verdict == VERDICT_FALSE_POSITIVE:
            counts = _bump(counts, "fp")
        elif row.verdict == VERDICT_TRUE_NEGATIVE:
            counts = _bump(counts, "tn")
    return counts


def _bump(counts: RawCounts, field: str) -> RawCounts:
    """给 ``RawCounts`` 的某一栏加一（frozen dataclass，只能整个换掉）。"""

    values = counts.to_dict()
    values[field] += 1
    return RawCounts(**values)


def count_rule_degraded(rows: Sequence[CaseRow]) -> int:
    """规则降级的例数 —— **结论不是模型给的**那些例。

    降级例**仍进指标分母**（结论是确定的，只是归并口径退到了规则基线），
    但要单列出来，复核时优先看。
    """

    return sum(
        1
        for row in rows
        if row.rule_degraded or row.error_code == ErrorCode.RULE_DEGRADED.value
    )


def count_model_normalized(rows: Sequence[CaseRow]) -> int:
    """用**模型**归并的例数 —— 与降级数并排看，才知道这轮有多少例的结论出自模型。"""

    return sum(1 for row in rows if row.normalize_source == NORMALIZE_SOURCE_MODEL)


# --------------------------------------------------------------------------
# 单项指标
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricValue:
    """一项指标的值 + 它的算式 + 分子分母。

    ``value`` 为 ``None`` 表示**算不出**（分母为 0），此时 ``reason`` 写明原因；
    ``value`` 绝不会是「分母为 0 时算出来的 0」。
    """

    name: str
    label: str
    value: float | None
    numerator: int
    denominator: int
    formula: str
    reason: str = ""

    @property
    def is_null(self) -> bool:
        return self.value is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "formula": self.formula,
            "reason": self.reason,
        }


def _round_half_even(value: Decimal) -> float:
    """四舍六入五入成双，保留四位小数。

    不用 ``round()``：它对二进制浮点的表示误差没有抵抗力
    （``round(0.1445, 4)`` 这类边界会算错一位），
    而这一栏是要给人**手算复核**的 —— 引擎算出来的数必须和计算器一致。
    """

    return float(value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN))


def _metric(
    name: str,
    label: str,
    formula: str,
    numerator: int,
    denominator: int,
) -> MetricValue:
    """算一项指标：分母为 0 → ``None`` + 原因，绝不返回 0。"""

    if denominator <= 0:
        return MetricValue(
            name=name,
            label=label,
            value=None,
            numerator=numerator,
            denominator=denominator,
            formula=formula,
            reason=_ZERO_DENOMINATOR_REASONS[name],
        )
    ratio = Decimal(numerator) / Decimal(denominator)
    return MetricValue(
        name=name,
        label=label,
        value=_round_half_even(ratio),
        numerator=numerator,
        denominator=denominator,
        formula=formula,
        reason="",
    )


def sensitivity_value(tp: int, fn: int) -> MetricValue:
    """敏感度 = TP / (TP + FN) —— 阳性样本里模型抓到了多少。"""

    return _metric("sensitivity", "敏感度", "TP/(TP+FN)", tp, tp + fn)


def specificity_value(tn: int, fp: int) -> MetricValue:
    """特异度 = TN / (TN + FP) —— 阴性样本里模型没乱报的有多少。"""

    return _metric("specificity", "特异度", "TN/(TN+FP)", tn, tn + fp)


def ppv_value(tp: int, fp: int) -> MetricValue:
    """阳性预测值 = TP / (TP + FP) —— 模型报出来的那些，有多少是真的。"""

    return _metric("ppv", "阳性预测值", "TP/(TP+FP)", tp, tp + fp)


def npv_value(tn: int, fn: int) -> MetricValue:
    """阴性预测值 = TN / (TN + FN) —— 模型说没事的那些，有多少是真的没事。"""

    return _metric("npv", "阴性预测值", "TN/(TN+FN)", tn, tn + fn)


def metric_values(counts: RawCounts) -> tuple[MetricValue, ...]:
    """四项指标按固定次序返回（敏感度 → 特异度 → PPV → NPV）。"""

    return (
        sensitivity_value(counts.tp, counts.fn),
        specificity_value(counts.tn, counts.fp),
        ppv_value(counts.tp, counts.fp),
        npv_value(counts.tn, counts.fn),
    )


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------


def compute_metrics(
    counts: RawCounts,
    *,
    rule_degraded: int = 0,
    model_normalized: int = 0,
) -> Metrics:
    """把原始计数折成 :class:`nodule_eval.contracts.Metrics`。

    产出里**同时**给两样东西：
    四项指标的值（算不出时为 ``None`` + 原因）与
    **原始计数 + 失败数 + 降级数 + 挂起数 + 不可比数** ——
    后者是给人手算复核用的，光给百分比没法核对。
    """

    sensitivity, specificity, ppv, npv = metric_values(counts)
    return Metrics(
        tp=counts.tp,
        fp=counts.fp,
        fn=counts.fn,
        tn=counts.tn,
        sensitivity=sensitivity.value,
        specificity=specificity.value,
        ppv=ppv.value,
        npv=npv.value,
        failed_count=counts.failed,
        rule_degraded_count=rule_degraded,
        needs_review_count=counts.needs_review,
        incomparable_count=counts.incomparable,
        model_normalized_count=model_normalized,
        sensitivity_reason=sensitivity.reason,
        specificity_reason=specificity.reason,
        ppv_reason=ppv.reason,
        npv_reason=npv.reason,
    )


def compute_metrics_from_rows(
    rows: Sequence[CaseRow],
    *,
    rule_degraded: int | None = None,
    model_normalized: int | None = None,
) -> Metrics:
    """从逐例行直接算出汇总指标（一站式入口，给测试与脚本用）。"""

    counts = count_raw_counts(rows)
    return compute_metrics(
        counts,
        rule_degraded=count_rule_degraded(rows) if rule_degraded is None else rule_degraded,
        model_normalized=(
            count_model_normalized(rows) if model_normalized is None else model_normalized
        ),
    )


# --------------------------------------------------------------------------
# 手算复核
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricsAudit:
    """汇总行与逐例行的**核对结果**。

    ``recomputed``
        从逐例行反算出来的原始计数。
    ``declared``
        汇总指标里写着的那份计数。
    ``differences``
        逐项差异（中文，写明差在哪一项、差多少）。
    """

    recomputed: RawCounts
    declared: RawCounts
    declared_metrics: Metrics
    recomputed_metrics: Metrics
    differences: tuple[str, ...] = ()

    @property
    def consistent(self) -> bool:
        return not self.differences

    def to_dict(self) -> dict[str, Any]:
        return {
            "consistent": self.consistent,
            "recomputed": self.recomputed.to_dict(),
            "declared": self.declared.to_dict(),
            "declared_metrics": self.declared_metrics.to_dict(),
            "recomputed_metrics": self.recomputed_metrics.to_dict(),
            "differences": list(self.differences),
        }


def audit_metrics(rows: Sequence[CaseRow], metrics: Metrics) -> MetricsAudit:
    """**手算复核入口**：给定逐例行与汇总指标，反算并逐项核对。

    核对两件事：

    1. 原始计数 —— 从逐例行反算出的 TP / FP / FN / TN 与汇总里写的逐项一致；
    2. 指标值 —— 由反算出的计数重算的四项指标与汇总里写的逐项一致
       （含「算不出（``null``）」这一态也要一致）。

    任何一项对不上，``differences`` 里点名差在哪。
    """

    recomputed = count_raw_counts(rows)
    declared = RawCounts(
        tp=metrics.tp,
        fp=metrics.fp,
        fn=metrics.fn,
        tn=metrics.tn,
        counted=metrics.tp + metrics.fp + metrics.fn + metrics.tn,
        failed=metrics.failed_count,
        needs_review=metrics.needs_review_count,
        incomparable=metrics.incomparable_count,
    )
    recomputed_metrics = compute_metrics(recomputed)
    differences: list[str] = []

    for field, label in (
        ("tp", "命中数 TP"),
        ("fp", "误报数 FP"),
        ("fn", "漏诊数 FN"),
        ("tn", "真阴性数 TN"),
        ("failed", "失败数"),
        ("needs_review", "挂起数"),
        ("incomparable", "不可比数"),
    ):
        got = getattr(recomputed, field)
        said = getattr(declared, field)
        if got != said:
            differences.append(f"{label}对不上：逐例行反算 {got}，汇总行写 {said}")

    for name in ("sensitivity", "specificity", "ppv", "npv"):
        expected = getattr(recomputed_metrics, name)
        actual = getattr(metrics, name)
        if expected != actual:
            differences.append(
                f"{name} 对不上：按反算的原始计数应为 {expected!r}，汇总行写 {actual!r}"
            )

    return MetricsAudit(
        recomputed=recomputed,
        declared=declared,
        declared_metrics=metrics,
        recomputed_metrics=recomputed_metrics,
        differences=tuple(differences),
    )


def assert_metrics_consistent(rows: Sequence[CaseRow], metrics: Metrics) -> None:
    """汇总行与逐例行对不上就当场报错 —— **不给一份看着正常的假汇总**。

    这条断言是给未来的改动设的绊子：哪天有人在汇总层又加了一套计数口径，
    汇总行就会和逐例明细对不上，人按汇总里的数复算、手算复核时才发现，
    那时候结论已经贴进招采说明了。
    """

    audit = audit_metrics(rows, metrics)
    if audit.consistent:
        return
    raise NoduleEvalError(
        ErrorCode.CONTRACT_INVALID,
        detail="汇总指标与逐例行对不上：" + "；".join(audit.differences),
    )


# --------------------------------------------------------------------------
# 汇总行文案
# --------------------------------------------------------------------------


def format_metric_value(value: float | None, reason: str = "") -> str:
    """指标值的表格文案：算得出写数值，算不出写 ``null`` + 原因。"""

    if value is None:
        return f"null（{reason}）" if reason else "null"
    return f"{value:.4f}"


def summary_row_text(metrics: Metrics) -> str:
    """汇总行的一行文案 —— 表格汇总区与终端输出共用同一句。

    顺序刻意是「原始计数 → 指标 → 各类单列」：
    复核的人先看到 TP/FP/FN/TN，再看百分比，最后才看到
    失败 / 降级 / 挂起 / 不可比这几栏 —— 一眼能判断这批数字该不该信。
    """

    return (
        f"TP {metrics.tp} ｜ FP {metrics.fp} ｜ FN {metrics.fn} ｜ TN {metrics.tn}"
        f" ｜ 敏感度 {format_metric_value(metrics.sensitivity, metrics.sensitivity_reason)}"
        f" ｜ 特异度 {format_metric_value(metrics.specificity, metrics.specificity_reason)}"
        f" ｜ 阳性预测值 {format_metric_value(metrics.ppv, metrics.ppv_reason)}"
        f" ｜ 阴性预测值 {format_metric_value(metrics.npv, metrics.npv_reason)}"
        f" ｜ 失败 {metrics.failed_count} ｜ 降级 {metrics.rule_degraded_count}"
        f" ｜ 挂起 {metrics.needs_review_count} ｜ 不可比 {metrics.incomparable_count}"
    )


def summary_row_dict(metrics: Metrics) -> dict[str, Any]:
    """汇总行的结构化版本（表格汇总区按单元格取值时用）。"""

    return {
        "tp": metrics.tp,
        "fp": metrics.fp,
        "fn": metrics.fn,
        "tn": metrics.tn,
        "counted": metrics.tp + metrics.fp + metrics.fn + metrics.tn,
        "sensitivity": metrics.sensitivity,
        "sensitivity_reason": metrics.sensitivity_reason,
        "specificity": metrics.specificity,
        "specificity_reason": metrics.specificity_reason,
        "ppv": metrics.ppv,
        "ppv_reason": metrics.ppv_reason,
        "npv": metrics.npv,
        "npv_reason": metrics.npv_reason,
        "failed_count": metrics.failed_count,
        "rule_degraded_count": metrics.rule_degraded_count,
        "needs_review_count": metrics.needs_review_count,
        "incomparable_count": metrics.incomparable_count,
        "model_normalized_count": metrics.model_normalized_count,
        "summary_text": summary_row_text(metrics),
    }
