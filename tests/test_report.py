"""任务 30：漏诊清单、结论报告与手算可复核对齐。

这一组断言回答科室评审会上最常问的三个问题：

1. **漏了几例？** —— 漏诊清单条数必须**恒等于**汇总行里的漏诊数 FN；
   复核的人数一遍清单，敏感度就能拿计算器手算对一遍。
2. **为什么漏？** —— 漏诊原因必须是**放射科看得懂的中文**，
   说清差在哪一层、没盖住的是哪一处，不是把字段名堆上去。
3. **这份结论能不能信？** —— ``summary.json`` 自带原始计数与算式，
   人拿计算器按 ``TP/(TP+FN)`` 对一遍就能复核；
   **对不上就报错并点名差在哪一项**，不给一份看着正常的假结论。

**端到端走真实出站**：推理端点是真监听 socket 的 HTTP 替身，
产品的 ``requests.Session`` / URL 拼接 / Bearer 头一行都没改。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    MISSED_LIST_COLUMNS,
    VERDICT_FALSE_POSITIVE,
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_TRUE_NEGATIVE,
    CaseInput,
    CaseRow,
    EvalRequest,
    Metrics,
    Summary,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.goldstd import normalize_gold_standard
from nodule_eval.metrics import (
    assert_summary_consistent,
    build_missed_list,
    build_summary_payload,
    compute_metrics_from_rows,
    gold_summary_text,
    manual_check_block,
    missed_not_counted,
    missed_reason_text,
    read_missed_list_csv,
    verdict_distribution,
    write_missed_list_csv,
)
from nodule_eval.store import RunStore

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"
#: 阴性金标准。
NEGATIVE_GOLD = "双肺未见明显结节影。"

#: 多标签金标准 —— 两处病灶（任务 28 的最严口径用这一句）。
MULTI_GOLD = "右下肺见一磨玻璃结节，大小约 8mm；左肺上叶见一实性结节，大小约 12mm。"


def _row(
    accession_no: str,
    verdict: str,
    *,
    held: bool = False,
    incomparable: bool = False,
    incomparable_reason: str = "",
    needs_review: bool = False,
    rule_degraded: bool = False,
    error_code: str = "",
    normalize_source: str = "",
    judge_basis: str = "",
    uncovered_labels: str = "",
    gold_summary: str = "",
    model_findings: str = "",
    tool_call_id: str = "",
) -> CaseRow:
    return CaseRow(
        accession_no=accession_no,
        verdict=verdict,
        held=held,
        incomparable=incomparable,
        incomparable_reason=incomparable_reason,
        needs_review=needs_review,
        rule_degraded=rule_degraded,
        error_code=error_code,
        normalize_source=normalize_source,
        judge_basis=judge_basis,
        uncovered_labels=uncovered_labels,
        gold_summary=gold_summary,
        model_findings=model_findings,
        tool_call_id=tool_call_id,
    )


def _sample_rows() -> list[CaseRow]:
    """一批覆盖四种判定 + 三类隔离例的逐例行。"""

    return [
        _row(
            "A1",
            VERDICT_HIT,
            judge_basis="存在性：一致；部位：一致；尺寸：一致；密度：一致",
            gold_summary="右下肺 8.0mm 磨玻璃",
            model_findings="右下肺 | 8.0mm | 磨玻璃 | 0.92",
            tool_call_id="call-1",
        ),
        _row(
            "A2",
            VERDICT_MISS,
            judge_basis="存在性：不一致；部位：未比对；尺寸：未比对；密度：未比对",
            gold_summary="右下肺 8.0mm 磨玻璃",
            model_findings="未检出候选病灶",
            tool_call_id="call-2",
        ),
        _row(
            "A3",
            VERDICT_FALSE_POSITIVE,
            judge_basis="存在性：不一致；部位：未比对；尺寸：未比对；密度：未比对",
            gold_summary="金标准写明未见结节",
            model_findings="右下肺 | 8.0mm | 磨玻璃 | 0.92",
            tool_call_id="call-3",
        ),
        _row(
            "A4",
            VERDICT_TRUE_NEGATIVE,
            judge_basis="存在性：一致；部位：未比对；尺寸：未比对；密度：未比对",
            gold_summary="金标准写明未见结节",
            model_findings="未检出候选病灶",
            tool_call_id="call-4",
        ),
        _row("A5", "failed", error_code="INFERENCE_5XX", judge_basis=""),
        _row("A6", VERDICT_HIT, held=True, judge_basis="存在性：一致"),
        _row(
            "A7",
            VERDICT_HIT,
            incomparable=True,
            incomparable_reason="部位写法「肺门区」认不出来",
            judge_basis="存在性：一致；部位：不可比",
        ),
    ]


def _summary(rows: Sequence[CaseRow], metrics: Metrics) -> Summary:
    return Summary(
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref="报告层验收样例",
        total_cases=len(rows),
        evaluated_cases=metrics.tp + metrics.fp + metrics.fn + metrics.tn,
        failed_count=metrics.failed_count,
        rule_degraded_count=metrics.rule_degraded_count,
        needs_review_count=metrics.needs_review_count,
        incomparable_count=metrics.incomparable_count,
        generated_at="2026-11-07T10:00:00+08:00",
        model_normalized_count=metrics.model_normalized_count,
    )


# --------------------------------------------------------------------------
# 一、漏诊清单：条数 ≡ 漏诊数 FN
# --------------------------------------------------------------------------


def test_missed_list_count_equals_fn():
    """清单条数与汇总行里的漏诊数必须相等 —— 复核的人就靠这一条。"""

    rows = _sample_rows()
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)

    assert metrics.fn == 1
    assert len(missed) == metrics.fn
    assert [item.accession_no for item in missed] == ["A2"]


def test_missed_list_excludes_failed_held_and_incomparable():
    """失败 / 挂起 / 不可比三类例**不进漏诊清单**（它们本来就不算漏诊）。"""

    rows = [
        _row("B1", "failed", error_code="INFERENCE_5XX"),
        _row("B2", VERDICT_HIT, held=True),
        _row("B3", VERDICT_MISS, incomparable=True, incomparable_reason="写法认不出来"),
        _row("B4", VERDICT_MISS),
    ]
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)

    assert metrics.fn == 1
    assert len(missed) == 1
    assert missed[0].accession_no == "B4"


def test_missed_but_not_counted_is_listed_separately():
    """判了漏诊却没进分母的例（属性不可比）**不静默丢** —— 单列出来。"""

    rows = [
        _row(
            "C1",
            VERDICT_MISS,
            incomparable=True,
            incomparable_reason="部位写法「肺门区」认不出来",
        ),
        _row("C2", VERDICT_MISS),
    ]
    missed = build_missed_list(rows)
    not_counted = missed_not_counted(rows)

    assert [item.accession_no for item in missed] == ["C2"]
    assert [entry["accession_no"] for entry in not_counted] == ["C1"]
    assert "肺门区" in not_counted[0]["reason"]


def test_missed_list_carries_tool_call_id_for_lookup():
    """漏诊清单必须带调用记录 ID —— 放射科要能反查回那次调用。"""

    rows = _sample_rows()
    missed = build_missed_list(rows)
    assert missed[0].tool_call_id == "call-2"


def test_missed_list_marks_rule_degraded_cases():
    """规则降级判出来的漏诊要单独标出来 —— 复核口径不同。"""

    rows = [_row("D1", VERDICT_MISS, rule_degraded=True, error_code="RULE_DEGRADED")]
    missed = build_missed_list(rows)
    assert missed[0].rule_degraded is True


# --------------------------------------------------------------------------
# 二、漏诊原因：放射科看得懂的中文
# --------------------------------------------------------------------------


def test_missed_reason_contains_uncovered_labels():
    """多标签最严口径的漏诊，原因里必须点名**没盖住的是哪一处**。"""

    row = _row(
        "E1",
        VERDICT_MISS,
        judge_basis="存在性：一致；部位：一致；尺寸：一致；密度：一致；覆盖：未覆盖（金标准 2 处病灶，模型盖住 1 处）",
        uncovered_labels="左肺上叶 12.0mm 实性（模型检出的部位与金标准这一处对不上）",
    )
    reason = missed_reason_text(row)

    assert "左肺上叶" in reason
    assert "12.0mm" in reason
    assert "实性" in reason
    assert "没盖住" in reason


def test_missed_reason_names_the_mismatched_layer():
    """属性层对不上时，原因要点名是**哪一层**。"""

    row = _row(
        "E2",
        VERDICT_MISS,
        judge_basis="存在性：一致；部位：不一致；尺寸：一致；密度：一致",
        uncovered_labels="左肺上叶 12.0mm 实性",
    )
    reason = missed_reason_text(row)
    assert "部位" in reason
    assert "不一致" in reason


def test_missed_reason_for_presence_mismatch():
    """模型压根没报出候选时，原因要说清差在「存在性」这一层。"""

    row = _row(
        "E3",
        VERDICT_MISS,
        judge_basis="存在性：不一致；部位：未比对；尺寸：未比对；密度：未比对",
    )
    reason = missed_reason_text(row)

    assert "存在性" in reason
    assert "没有报出" in reason


def test_missed_reason_is_readable_chinese_not_field_names():
    """原因里**不出现内部字段名** —— 放射科看不懂 ``uncovered_labels``。"""

    row = _row(
        "E4",
        VERDICT_MISS,
        judge_basis="存在性：一致；部位：一致；尺寸：一致；密度：一致",
        uncovered_labels="左肺上叶 12.0mm 实性",
    )
    reason = missed_reason_text(row)
    for token in ("uncovered_labels", "judge_basis", "gold_summary", "tool_call_id"):
        assert token not in reason


# --------------------------------------------------------------------------
# 三、summary.json：run 级结论 + 手算复核块
# --------------------------------------------------------------------------


def test_summary_payload_carries_run_conclusion():
    """结论里要有招采 / 换版说明要引用的全部字段。"""

    rows = _sample_rows()
    metrics = compute_metrics_from_rows(rows)
    summary = _summary(rows, metrics)
    missed = build_missed_list(rows)

    payload = build_summary_payload(
        run_id="run-report-001",
        summary=summary,
        metrics=metrics,
        rows=rows,
        missed=missed,
        missed_list_path="missed_list.csv",
    )

    assert payload["run_id"] == "run-report-001"
    assert payload["model_id"] == "demo-model"
    assert payload["model_version"] == "v1.0.0"
    assert payload["dataset_ref"] == "报告层验收样例"
    assert payload["total_cases"] == 7
    assert payload["evaluated_cases"] == 4
    assert payload["generated_at"] == "2026-11-07T10:00:00+08:00"
    # 四项指标
    assert payload["metrics"]["sensitivity"] == 0.5
    assert payload["metrics"]["specificity"] == 0.5
    assert payload["metrics"]["ppv"] == 0.5
    assert payload["metrics"]["npv"] == 0.5
    # 失败数 / 降级数 / 挂起数
    assert payload["counts"]["failed"] == 1
    assert payload["counts"]["needs_review"] == 1
    assert payload["counts"]["incomparable"] == 1
    assert payload["counts"]["rule_degraded"] == 0
    # 汇总行一行文案
    assert "TP 1" in payload["summary_text"]


def test_summary_payload_manual_check_recomputes_consistently():
    """手算复核块：从逐例行反算回原始计数，与汇总行写的逐项一致。"""

    rows = _sample_rows()
    metrics = compute_metrics_from_rows(rows)
    block = manual_check_block(rows, metrics)

    assert block["consistent"] is True
    assert block["differences"] == []
    assert block["declared"] == block["recomputed"]


def test_summary_payload_formulas_are_hand_checkable():
    """算式连中间值都写出来 —— 拿计算器按这一行就能对一遍。"""

    rows = [
        _row("F1", VERDICT_HIT),
        _row("F2", VERDICT_HIT),
        _row("F3", VERDICT_MISS),
        _row("F4", VERDICT_TRUE_NEGATIVE),
    ]
    metrics = compute_metrics_from_rows(rows)
    block = manual_check_block(rows, metrics)

    assert block["formulas"]["sensitivity"] == "TP/(TP+FN) = 2/3 = 0.6667"
    assert block["formulas"]["specificity"] == "TN/(TN+FP) = 1/1 = 1.0000"
    assert block["formulas"]["ppv"] == "TP/(TP+FP) = 2/2 = 1.0000"
    assert block["formulas"]["npv"] == "TN/(TN+FN) = 1/2 = 0.5000"


def test_summary_payload_records_null_metric_with_reason():
    """算不出的指标是 ``null`` + 原因，算式里也写明 —— 不是 0。"""

    rows = [_row("G1", VERDICT_TRUE_NEGATIVE)]
    metrics = compute_metrics_from_rows(rows)
    assert metrics.sensitivity is None

    payload = build_summary_payload(
        run_id="run-null",
        summary=_summary(rows, metrics),
        metrics=metrics,
        rows=rows,
        missed=build_missed_list(rows),
    )
    assert payload["metrics"]["sensitivity"] is None
    assert payload["metrics"]["sensitivity_reason"]
    assert "null" in payload["manual_check"]["formulas"]["sensitivity"]


def test_summary_payload_missed_block_matches_fn():
    """结论里的漏诊块与 FN 对得上，并指明清单在哪。"""

    rows = _sample_rows()
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)
    payload = build_summary_payload(
        run_id="run-missed",
        summary=_summary(rows, metrics),
        metrics=metrics,
        rows=rows,
        missed=missed,
        missed_list_path="missed_list.csv",
    )

    assert payload["missed"]["count"] == metrics.fn == 1
    assert payload["missed"]["fn"] == 1
    assert payload["missed"]["list_path"] == "missed_list.csv"
    assert payload["missed"]["not_counted"] == []


def test_summary_payload_has_verdict_distribution():
    """五种判定各多少例 —— **按判定值统计**（与表上「判定」列一致）。

    挂起例与不可比例的判定值也照数：表上就是这么写的；
    真正进了 TP / FP / FN / TN 的是哪些，看 ``counts``。
    """

    rows = _sample_rows()
    distribution = verdict_distribution(rows)
    assert distribution[VERDICT_HIT] == 3  # A1 + 挂起的 A6 + 不可比的 A7
    assert distribution[VERDICT_MISS] == 1
    assert distribution[VERDICT_FALSE_POSITIVE] == 1
    assert distribution[VERDICT_TRUE_NEGATIVE] == 1
    assert distribution["failed"] == 1


# --------------------------------------------------------------------------
# 四、手算对齐检查：对不上就报错并点名差异项
# --------------------------------------------------------------------------


def test_assert_summary_consistent_passes_on_good_data():
    rows = _sample_rows()
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)
    assert_summary_consistent(rows, metrics, _summary(rows, metrics), missed)


def test_assert_summary_consistent_detects_tampered_metrics():
    """指标被改过 → 当场报错并点名差在哪一项。"""

    rows = _sample_rows()
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)
    tampered = Metrics(
        tp=9,
        fp=metrics.fp,
        fn=metrics.fn,
        tn=metrics.tn,
        sensitivity=0.9,
        specificity=metrics.specificity,
        ppv=metrics.ppv,
        npv=metrics.npv,
        failed_count=metrics.failed_count,
    )

    with pytest.raises(NoduleEvalError) as excinfo:
        assert_summary_consistent(rows, tampered, _summary(rows, metrics), missed)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID.value
    assert "命中数 TP" in excinfo.value.message


def test_assert_summary_consistent_detects_missed_count_drift():
    """漏诊清单条数与 FN 对不上 → 报错并点名「漏诊清单条数」。"""

    rows = _sample_rows()
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)
    # 偷偷塞一条不在明细里的漏诊 —— 清单会多出一条。
    tampered = missed + (missed[0],)

    with pytest.raises(NoduleEvalError) as excinfo:
        assert_summary_consistent(rows, metrics, _summary(rows, metrics), tampered)
    assert "漏诊清单条数" in excinfo.value.message


def test_assert_summary_consistent_detects_bucket_drift():
    """单列栏（失败数）对不上 → 报错并点名「失败数」。"""

    rows = _sample_rows()
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)
    summary = _summary(rows, metrics)
    broken = Summary(
        model_id=summary.model_id,
        model_version=summary.model_version,
        dataset_ref=summary.dataset_ref,
        total_cases=summary.total_cases,
        evaluated_cases=summary.evaluated_cases,
        failed_count=0,  # 汇总说 0 例失败，逐例行里明明有 1 例
        rule_degraded_count=summary.rule_degraded_count,
        needs_review_count=summary.needs_review_count,
        incomparable_count=summary.incomparable_count,
        generated_at=summary.generated_at,
        model_normalized_count=summary.model_normalized_count,
    )

    with pytest.raises(NoduleEvalError) as excinfo:
        assert_summary_consistent(rows, metrics, broken, missed)
    assert "失败数" in excinfo.value.message


def test_assert_summary_consistent_detects_case_count_drift():
    """样本量与逐例行条数对不上 → 报错并点名「病例数」。"""

    rows = _sample_rows()
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)
    summary = _summary(rows, metrics)
    broken = Summary(
        model_id=summary.model_id,
        model_version=summary.model_version,
        dataset_ref=summary.dataset_ref,
        total_cases=99,  # 说 99 例，逐例行只有 7 行
        evaluated_cases=summary.evaluated_cases,
        failed_count=summary.failed_count,
        rule_degraded_count=summary.rule_degraded_count,
        needs_review_count=summary.needs_review_count,
        incomparable_count=summary.incomparable_count,
        generated_at=summary.generated_at,
        model_normalized_count=summary.model_normalized_count,
    )

    with pytest.raises(NoduleEvalError) as excinfo:
        assert_summary_consistent(rows, metrics, broken, missed)
    assert "病例数" in excinfo.value.message


# --------------------------------------------------------------------------
# 五、漏诊清单 CSV：列序、编码、可读回
# --------------------------------------------------------------------------


def test_missed_list_csv_has_fixed_columns_and_excel_friendly_encoding(tmp_path):
    """列序固定 + ``utf-8-sig`` —— 科室双击用 Excel 打开不乱码。"""

    rows = _sample_rows()
    missed = build_missed_list(rows)
    path = write_missed_list_csv(tmp_path / "missed_list.csv", missed)

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "Excel 直接打开不乱码的 utf-8-sig"
    header = raw.decode("utf-8-sig").splitlines()[0]
    assert header.split(",") == list(MISSED_LIST_COLUMNS)


def test_missed_list_csv_round_trips(tmp_path):
    """写出去再读回来，逐字一致（表头缺列即报契约错，不静默补空）。"""

    rows = _sample_rows()
    missed = build_missed_list(rows)
    path = write_missed_list_csv(tmp_path / "missed_list.csv", missed)
    read_back = read_missed_list_csv(path)

    assert len(read_back) == len(missed)
    assert read_back[0]["accession_no"] == missed[0].accession_no
    assert read_back[0]["missed_reason"] == missed[0].missed_reason
    assert read_back[0]["gold_standard_summary"] == missed[0].gold_standard_summary


def test_missed_list_csv_missing_column_is_reported(tmp_path):
    """表头缺列 → 报契约错并点名缺哪一列。"""

    path = tmp_path / "broken.csv"
    path.write_text("accession_no,model_findings\nA1,x\n", encoding="utf-8")
    with pytest.raises(NoduleEvalError) as excinfo:
        read_missed_list_csv(path)
    assert "缺列" in excinfo.value.message
    assert "missed_reason" in excinfo.value.message


# --------------------------------------------------------------------------
# 六、产物不落金标准原文
# --------------------------------------------------------------------------


def test_gold_summary_is_built_from_labels_not_raw_text():
    """金标准摘要是**归并后的标准标签**，不是原文摘抄。

    「右下肺」按部位等价表归一成标准标签「右肺下叶」——
    摘要给的是**归一后的**标签，金标准侧与模型侧用的是同一张表。
    """

    gold = normalize_gold_standard(POSITIVE_GOLD)
    summary = gold_summary_text(gold)

    assert "右肺下叶" in summary
    assert "8.0mm" in summary
    assert "磨玻璃" in summary
    # 原文里的标点与句号不该进摘要
    assert "。" not in summary


def test_gold_summary_never_carries_patient_identifiers():
    """原文带姓名 / 住院号时，摘要里**不出现**这些可识别信息。"""

    gold = normalize_gold_standard("患者张某某，住院号 0012345，右下肺见一磨玻璃结节，大小约 8mm。")
    summary = gold_summary_text(gold)

    assert "张某某" not in summary
    assert "0012345" not in summary
    assert "右肺下叶" in summary
    assert "磨玻璃" in summary


def test_summary_payload_contains_no_raw_gold_text():
    """结论与漏诊清单里都不出现金标准原文（只出现结构化摘要）。"""

    rows = [
        _row(
            "H1",
            VERDICT_MISS,
            gold_summary="右下肺 8.0mm 磨玻璃",
            model_findings="未检出候选病灶",
            judge_basis="存在性：不一致；部位：未比对；尺寸：未比对；密度：未比对",
        )
    ]
    metrics = compute_metrics_from_rows(rows)
    missed = build_missed_list(rows)
    payload = build_summary_payload(
        run_id="run-raw",
        summary=_summary(rows, metrics),
        metrics=metrics,
        rows=rows,
        missed=missed,
    )

    text = json.dumps(payload, ensure_ascii=False)
    assert "见一磨玻璃结节" not in text
    # 漏诊清单带的是结构化摘要，不是原文
    assert missed[0].gold_standard_summary == "右下肺 8.0mm 磨玻璃"
    assert "见一磨玻璃结节" not in missed[0].gold_standard_summary


# --------------------------------------------------------------------------
# 七、端到端：引擎真外发，产物落盘
# --------------------------------------------------------------------------


def _engine(tmp_path: Path) -> Engine:
    config = load_config()
    spec = load_model_spec("demo-model")
    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    engine = Engine(
        config,
        spec,
        client=HttpClient(sleep=lambda _seconds: None),
        runs_dir=tmp_path / "runs",
        store=store,
    )
    engine.test_store = store  # type: ignore[attr-defined]
    return engine


def _cases(count: int, gold: str = POSITIVE_GOLD) -> tuple[CaseInput, ...]:
    return tuple(
        CaseInput(
            accession_no=f"ACC-{index:04d}",
            gold_standard_text=gold,
            image_ref=f"dx://local/{index:04d}",
            study_uid=f"STUDY-{index:04d}",
            row_no=index + 1,
        )
        for index in range(count)
    )


def _request(cases: Sequence[CaseInput], *, run_id: str) -> EvalRequest:
    return EvalRequest(
        run_id=run_id,
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref="报告层端到端样例",
        cases=tuple(cases),
    )


def test_engine_writes_missed_list_and_summary(tmp_path, full_env, monkeypatch):
    """跑一轮真外发：漏诊清单与结论都落盘，且清单条数 ≡ FN。"""

    # 替身回「未检出候选」→ 金标准说有结节 → 存在性不一致 → 漏诊。
    stub = InferenceStub(candidates=[])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(3), run_id="report-e2e"))
    finally:
        stub.stop()

    run_dir = tmp_path / "runs" / "report-e2e"
    missed_path = run_dir / "missed_list.csv"
    summary_path = run_dir / "summary.json"

    assert missed_path.is_file()
    assert summary_path.is_file()

    # 三例全是漏诊 → 清单三条、FN 三
    assert response.metrics.fn == 3
    assert len(response.missed_list) == 3

    read_back = read_missed_list_csv(missed_path)
    assert len(read_back) == 3
    assert {row["accession_no"] for row in read_back} == {
        "ACC-0000",
        "ACC-0001",
        "ACC-0002",
    }
    # 清单带调用记录 ID 与漏判原因
    assert all(row["tool_call_id"] for row in read_back)
    assert all("存在性" in row["missed_reason"] for row in read_back)

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "report-e2e"
    assert payload["model_id"] == "demo-model"
    assert payload["counts"]["fn"] == 3
    assert payload["missed"]["count"] == 3
    assert payload["manual_check"]["consistent"] is True
    # 敏感度 = 0/3 = 0（这一轮模型一例都没检出，是货真价实的 0）
    assert payload["metrics"]["sensitivity"] == 0.0
    assert payload["metrics"]["specificity"] is None

    engine.test_store.close()  # type: ignore[attr-defined]


def test_engine_missed_list_is_empty_when_no_miss(tmp_path, full_env, monkeypatch):
    """一例都没漏时，清单只有表头 —— 不塞占位行。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(2), run_id="report-e2e-clean"))
    finally:
        stub.stop()

    assert response.metrics.fn == 0
    assert response.missed_list == ()

    read_back = read_missed_list_csv(tmp_path / "runs" / "report-e2e-clean" / "missed_list.csv")
    assert read_back == ()

    engine.test_store.close()  # type: ignore[attr-defined]


def test_engine_summary_json_is_consistent_with_case_rows(tmp_path, full_env, monkeypatch):
    """结论里的原始计数与逐例明细 CSV 反算一致（跨产物核对）。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(2), run_id="report-e2e-x"))
    finally:
        stub.stop()

    run_dir = tmp_path / "runs" / "report-e2e-x"
    payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert payload["counts"]["tp"] == response.metrics.tp
    assert payload["counts"]["fn"] == response.metrics.fn
    assert payload["counts"]["tn"] == response.metrics.tn
    assert payload["evaluated_cases"] == payload["counts"]["counted"]

    engine.test_store.close()  # type: ignore[attr-defined]
