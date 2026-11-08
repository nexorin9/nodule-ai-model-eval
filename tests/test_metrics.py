"""任务 29：指标计算与汇总行原始计数（null 语义）测试。

这一组测试盯住三件事：

1. **四项指标算得对** —— 与人工手算逐位一致；
2. **分母为 0 时是 ``null`` 而不是 0** —— 并且写明原因；
3. **汇总行能被反算复核** —— 给定逐例行与汇总指标，
   能反算回 TP / FP / FN / TN 并逐项核对，对不上就点名差在哪。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from nodule_eval.contracts import (
    VERDICT_FALSE_POSITIVE,
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_TRUE_NEGATIVE,
    CaseRow,
    Metrics,
)
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.metrics import (
    METRIC_DECIMALS,
    RawCounts,
    assert_metrics_consistent,
    audit_metrics,
    compute_metrics,
    compute_metrics_from_rows,
    count_raw_counts,
    count_rule_degraded,
    format_metric_value,
    metric_values,
    npv_value,
    ppv_value,
    sensitivity_value,
    specificity_value,
    summary_row_dict,
    summary_row_text,
)


def _row(
    accession_no: str,
    verdict: str,
    *,
    held: bool = False,
    incomparable: bool = False,
    needs_review: bool = False,
    rule_degraded: bool = False,
    error_code: str = "",
    normalize_source: str = "",
) -> CaseRow:
    return CaseRow(
        accession_no=accession_no,
        verdict=verdict,
        held=held,
        incomparable=incomparable,
        needs_review=needs_review,
        rule_degraded=rule_degraded,
        error_code=error_code,
        normalize_source=normalize_source,
    )


# --------------------------------------------------------------------------
# 一、四项指标：与手算一致
# --------------------------------------------------------------------------


def test_four_metrics_match_hand_calculation():
    """TP=18 FP=2 FN=3 TN=25 → 手算 0.8571 / 0.9259 / 0.9 / 0.8929。"""

    counts = RawCounts(tp=18, fp=2, fn=3, tn=25, counted=48)
    metrics = compute_metrics(counts)

    assert metrics.tp == 18
    assert metrics.fp == 2
    assert metrics.fn == 3
    assert metrics.tn == 25
    # 敏感度 18/(18+3)=0.857142… → 0.8571
    assert metrics.sensitivity == 0.8571
    # 特异度 25/(25+2)=0.925925… → 0.9259
    assert metrics.specificity == 0.9259
    # 阳性预测值 18/(18+2)=0.9
    assert metrics.ppv == 0.9
    # 阴性预测值 25/(25+3)=0.892857… → 0.8929
    assert metrics.npv == 0.8929


def test_metric_values_carry_formula_and_fraction():
    """每项指标都带着自己的算式与分子分母 —— 复核的人不用回头翻代码。"""

    value = sensitivity_value(18, 3)
    assert value.formula == "TP/(TP+FN)"
    assert value.numerator == 18
    assert value.denominator == 21
    assert value.label == "敏感度"
    assert value.is_null is False
    assert value.reason == ""


def test_metric_values_order_is_fixed():
    values = metric_values(RawCounts(tp=1, fp=1, fn=1, tn=1))
    assert [v.name for v in values] == ["sensitivity", "specificity", "ppv", "npv"]


def test_metrics_keep_four_decimal_places():
    """指标保留四位小数（:data:`METRIC_DECIMALS`），原始计数保持整数。"""

    metrics = compute_metrics(RawCounts(tp=1, fp=1, fn=1, tn=1, counted=4))
    for value in (metrics.sensitivity, metrics.specificity, metrics.ppv, metrics.npv):
        assert value is not None
        assert round(value, METRIC_DECIMALS) == value
        assert len(str(value).split(".")[1]) <= METRIC_DECIMALS
    assert all(isinstance(v, int) for v in (metrics.tp, metrics.fp, metrics.fn, metrics.tn))


def test_rounding_is_half_even_not_binary_float():
    """四舍六入五入成双：0.12345 → 0.1234（不是二进制浮点的 0.1234/0.1235 抖动）。

    用 Decimal 直接算一遍做对照，确认引擎给出的数与手算一致。
    """

    # 1/8 = 0.125 整，除不尽的位数交给量化处理
    metrics = compute_metrics(RawCounts(tp=1, fp=0, fn=7, tn=0, counted=8))
    assert metrics.sensitivity == 0.125

    # 0.1445 → 0.1444（银行家舍入：前一位是偶数则舍去）
    # 29/200 = 0.145 → 0.1450（恰在位，不动）
    metrics = compute_metrics(RawCounts(tp=29, fp=0, fn=171, tn=0, counted=200))
    expected = float(
        (Decimal(29) / Decimal(200)).quantize(Decimal("0.0001"), rounding="ROUND_HALF_EVEN")
    )
    assert metrics.sensitivity == expected


# --------------------------------------------------------------------------
# 二、分母为 0 → null，绝不输出 0
# --------------------------------------------------------------------------


def test_zero_denominator_yields_null_not_zero():
    """没有阳性样本时敏感度是 ``None``，**不是 0**。"""

    metrics = compute_metrics(RawCounts(tp=0, fp=2, fn=0, tn=3, counted=5))
    assert metrics.sensitivity is None
    assert metrics.sensitivity != 0
    assert metrics.specificity == 0.6


def test_zero_denominator_writes_reason():
    """分母为 0 时写明原因，让看表的人知道这一栏是「算不出」。"""

    metrics = compute_metrics(RawCounts(tp=0, fp=0, fn=0, tn=0, counted=0))
    for reason in (
        metrics.sensitivity_reason,
        metrics.specificity_reason,
        metrics.ppv_reason,
        metrics.npv_reason,
    ):
        assert reason
        assert "分母为 0" in reason
        assert "不是 0" in reason


def test_zero_denominator_does_not_affect_other_metrics():
    """一项指标算不出，不影响另外三项。

    注意区分两种「0」：敏感度分母（金标准阳性）为 0 → **算不出（null）**；
    阳性预测值分母（模型报出的例数）为 4 → **算得出，是 0.0**
    —— 模型报了 4 个结节，一个都没对，这就是货真价实的 0。
    """

    metrics = compute_metrics(RawCounts(tp=0, fp=4, fn=0, tn=6, counted=10))
    assert metrics.sensitivity is None
    assert metrics.specificity == 0.6
    assert metrics.ppv == 0.0  # TP/(TP+FP) = 0/4
    assert metrics.npv == 1.0  # TN/(TN+FN) = 6/6


def test_empty_run_all_metrics_null():
    """一轮都没跑成时，四项指标全为 ``null``，原始计数全为 0。"""

    metrics = compute_metrics(RawCounts())
    assert metrics.sensitivity is None
    assert metrics.specificity is None
    assert metrics.ppv is None
    assert metrics.npv is None
    assert (metrics.tp, metrics.fp, metrics.fn, metrics.tn) == (0, 0, 0, 0)


def test_format_metric_value_shows_reason_for_null():
    assert format_metric_value(0.8571) == "0.8571"
    text = format_metric_value(None, "无阳性样本，分母为 0")
    assert text.startswith("null")
    assert "无阳性样本" in text


def test_metric_values_null_semantics_for_each():
    assert sensitivity_value(0, 0).value is None
    assert specificity_value(0, 0).value is None
    assert ppv_value(0, 0).value is None
    assert npv_value(0, 0).value is None


# --------------------------------------------------------------------------
# 三、汇总行：原始计数 + 各栏单列
# --------------------------------------------------------------------------


def test_summary_row_carries_all_raw_counts_and_buckets():
    """汇总行必须同时给 TP/FP/FN/TN + 失败 + 降级 + 挂起 + 不可比。"""

    rows = [
        _row("A1", VERDICT_HIT),
        _row("A2", VERDICT_HIT),
        _row("A3", VERDICT_MISS),
        _row("A4", VERDICT_FALSE_POSITIVE),
        _row("A5", VERDICT_TRUE_NEGATIVE),
        _row("A6", "failed"),
        _row("A7", VERDICT_HIT, held=True),
        _row("A8", VERDICT_HIT, incomparable=True),
    ]
    metrics = compute_metrics_from_rows(rows)

    assert (metrics.tp, metrics.fp, metrics.fn, metrics.tn) == (2, 1, 1, 1)
    assert metrics.failed_count == 1
    assert metrics.needs_review_count == 1
    assert metrics.incomparable_count == 1
    # 降级例仍进分母，但单列
    assert metrics.rule_degraded_count == 0


def test_raw_counts_exclude_failed_held_and_incomparable():
    """失败 / 挂起 / 不可比三類例**一律不进分母**，各单列。"""

    rows = [
        _row("A1", VERDICT_HIT),
        _row("A2", "failed"),
        _row("A3", VERDICT_HIT, held=True),
        _row("A4", VERDICT_HIT, incomparable=True),
        _row("A5", VERDICT_TRUE_NEGATIVE),
    ]
    counts = count_raw_counts(rows)
    assert counts.tp == 1
    assert counts.tn == 1
    assert counts.counted == 2
    assert counts.failed == 1
    assert counts.needs_review == 1
    assert counts.incomparable == 1
    assert counts.tp + counts.fp + counts.fn + counts.tn == counts.counted


def test_rule_degraded_case_stays_in_denominator_and_counted():
    """规则降级的例**仍进分母**（结论是确定的），但要单列降级数。"""

    rows = [
        _row("A1", VERDICT_HIT, rule_degraded=True, error_code="RULE_DEGRADED"),
        _row("A2", VERDICT_HIT),
    ]
    counts = count_raw_counts(rows)
    assert counts.tp == 2
    assert counts.counted == 2
    assert count_rule_degraded(rows) == 1


def test_summary_text_contains_counts_and_metrics():
    """汇总行文案：原始计数 → 指标 → 单列各栏，一行看全。"""

    metrics = compute_metrics(RawCounts(tp=18, fp=2, fn=3, tn=25, counted=48))
    text = summary_row_text(metrics)
    assert "TP 18" in text
    assert "FP 2" in text
    assert "FN 3" in text
    assert "TN 25" in text
    assert "0.8571" in text
    assert "失败 0" in text
    assert "挂起 0" in text
    assert "不可比 0" in text


def test_summary_dict_carries_null_and_reason_together():
    """结构化汇总行里，``null`` 与它的原因成对出现。"""

    metrics = compute_metrics(RawCounts(tp=0, fp=0, fn=0, tn=5, counted=5))
    payload = summary_row_dict(metrics)
    assert payload["sensitivity"] is None
    assert payload["sensitivity_reason"]
    assert payload["counted"] == 5
    assert payload["summary_text"]


# --------------------------------------------------------------------------
# 四、手算复核：反算回 TP/FP/FN/TN 并核对
# --------------------------------------------------------------------------


def test_audit_recomputes_and_finds_consistent():
    """给定逐例行与汇总指标，能反算回原始计数并核对一致。"""

    rows = [
        _row("A1", VERDICT_HIT),
        _row("A2", VERDICT_MISS),
        _row("A3", VERDICT_FALSE_POSITIVE),
        _row("A4", VERDICT_TRUE_NEGATIVE),
        _row("A5", "failed"),
    ]
    metrics = compute_metrics_from_rows(rows)
    audit = audit_metrics(rows, metrics)

    assert audit.consistent is True
    assert audit.differences == ()
    assert audit.recomputed.tp == 1
    assert audit.recomputed.fp == 1
    assert audit.recomputed.fn == 1
    assert audit.recomputed.tn == 1
    assert audit.recomputed.failed == 1


def test_audit_detects_tampered_counts():
    """汇总里的计数被改过 → 复核当场点名差在哪一项。"""

    rows = [_row("A1", VERDICT_HIT), _row("A2", VERDICT_MISS)]
    metrics = compute_metrics_from_rows(rows)
    tampered = Metrics(
        tp=9,
        fp=metrics.fp,
        fn=metrics.fn,
        tn=metrics.tn,
        sensitivity=metrics.sensitivity,
        specificity=metrics.specificity,
        ppv=metrics.ppv,
        npv=metrics.npv,
        failed_count=metrics.failed_count,
        rule_degraded_count=metrics.rule_degraded_count,
        needs_review_count=metrics.needs_review_count,
        incomparable_count=metrics.incomparable_count,
        model_normalized_count=metrics.model_normalized_count,
    )

    audit = audit_metrics(rows, tampered)
    assert audit.consistent is False
    assert any("命中数 TP" in diff for diff in audit.differences)


def test_assert_metrics_consistent_raises_on_mismatch():
    """对不上就抛 ``CONTRACT_INVALID``，不放过一份假汇总。"""

    rows = [_row("A1", VERDICT_HIT)]
    metrics = compute_metrics_from_rows(rows)
    bad = Metrics(tp=5, fp=0, fn=0, tn=0, sensitivity=1.0)

    with pytest.raises(NoduleEvalError) as excinfo:
        assert_metrics_consistent(rows, bad)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID.value
    assert "汇总指标与逐例行对不上" in excinfo.value.message


def test_assert_metrics_consistent_passes_on_good_metrics():
    rows = [_row("A1", VERDICT_HIT), _row("A2", "failed")]
    assert_metrics_consistent(rows, compute_metrics_from_rows(rows))


def test_audit_detects_tampered_metric_value():
    """原始计数对得上、但指标值被手改过 → 也要报出来。"""

    rows = [_row("A1", VERDICT_HIT), _row("A2", VERDICT_MISS)]
    metrics = compute_metrics_from_rows(rows)
    tampered = Metrics(
        tp=metrics.tp,
        fp=metrics.fp,
        fn=metrics.fn,
        tn=metrics.tn,
        sensitivity=0.99,
        specificity=metrics.specificity,
        ppv=metrics.ppv,
        npv=metrics.npv,
    )
    audit = audit_metrics(rows, tampered)
    assert audit.consistent is False
    assert any("sensitivity" in diff for diff in audit.differences)


def test_audit_detects_null_state_mismatch():
    """「算不出」与「算出来是 0」也必须分得清 —— 复核要能发现这种偷换。"""

    rows = [_row("A1", VERDICT_TRUE_NEGATIVE)]  # 没有阳性样本 → 敏感度算不出
    metrics = compute_metrics_from_rows(rows)
    assert metrics.sensitivity is None

    tampered = Metrics(
        tp=0,
        fp=0,
        fn=0,
        tn=1,
        sensitivity=0.0,  # 把「算不出」偷换成「算出来是 0」
        specificity=metrics.specificity,
        ppv=metrics.ppv,
        npv=metrics.npv,
    )
    audit = audit_metrics(rows, tampered)
    assert audit.consistent is False
    assert any("sensitivity" in diff for diff in audit.differences)


def test_audit_detects_bucket_count_drift():
    """失败数 / 挂起数 / 不可比数对不上，同样要报。"""

    rows = [_row("A1", VERDICT_HIT), _row("A2", "failed")]
    metrics = compute_metrics_from_rows(rows)
    tampered = Metrics(
        tp=metrics.tp,
        fp=metrics.fp,
        fn=metrics.fn,
        tn=metrics.tn,
        sensitivity=metrics.sensitivity,
        specificity=metrics.specificity,
        ppv=metrics.ppv,
        npv=metrics.npv,
        failed_count=0,  # 汇总说 0 例失败，逐例行里明明有 1 例
    )
    audit = audit_metrics(rows, tampered)
    assert audit.consistent is False
    assert any("失败数" in diff for diff in audit.differences)
