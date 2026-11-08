"""多标签最严口径与失败隔离边界（任务 28）。

这一组断言回答三个问题 —— 每一个都会**直接改写敏感度与特异度**：

1. **金标准写了几处，模型盖住了几处** —— 只盖住一半算不算命中？
   （最严口径：任一处没盖住即判漏诊，避免高估。）
2. **没盖住的是哪一处** —— 复核的人必须一眼看出「漏的是左下肺那一处」，
   而不是只看到一个 ``miss``。
3. **不可比与失败是不是被混进同一栏** —— 写法认不出的例（不可比）
   与调用失败的例（failed）**都不是命中也不是漏诊**，
   两者**各自单列**，都不进 TP / FP / FN / TN。

**为什么边界值得逐条钉死**：把「金标准三处、模型盖住两处」判成命中，
敏感度会凭空抬高；把「写法认不出」判成漏诊，漏诊清单里会多出
根本不是模型问题的条目 —— 两种错法在汇总行上都只差一个数字。
"""

from __future__ import annotations

import json

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    VERDICT_FAILED,
    VERDICT_HIT,
    VERDICT_MISS,
    CaseInput,
    CaseRow,
    EvalRequest,
    RunOptions,
    counts_toward_denominator,
    partition_rows,
)
from nodule_eval.engine import Engine, count_metric_rows
from nodule_eval.errors import ErrorCode
from nodule_eval.goldstd import GoldLabels
from nodule_eval.goldstd.schema import LesionLabels
from nodule_eval.metrics import (
    COVERAGE_COVERED,
    COVERAGE_INCOMPARABLE,
    COVERAGE_UNCOVERED,
    LAYER_INCOMPARABLE,
    LAYER_MISMATCH,
    compare_coverage,
    judge_case,
)
from nodule_eval.store import RunStore


class _Candidate:
    """一个候选病灶（只带比对要用的三个属性）。"""

    def __init__(
        self,
        position: str,
        size_mm: float | None,
        density: str,
        confidence: float = 0.9,
    ):
        self.position = position
        self.size_mm = size_mm
        self.density = density
        self.confidence = confidence


#: 金标准写了两处病灶：右肺上叶 5mm 实性 + 左肺下叶 12mm 磨玻璃。
GOLD_TWO_LESIONS = GoldLabels(
    presence=True,
    location="右肺上叶",
    size_mm=5.0,
    density="实性",
    lesions=(
        LesionLabels(location="右肺上叶", size_mm=5.0, density="实性", finding="实性结节"),
        LesionLabels(location="左肺下叶", size_mm=12.0, density="磨玻璃", finding="磨玻璃结节"),
    ),
)

#: 只写了一处（右肺下叶 8mm 磨玻璃）—— 最常见的情形。
GOLD_ONE_LESION = GoldLabels(
    presence=True,
    location="右肺下叶",
    size_mm=8.0,
    density="磨玻璃",
)


# --------------------------------------------------------------------------
# 1. 多标签最严口径：全覆盖 → 命中，部分覆盖 → 漏诊
# --------------------------------------------------------------------------
def test_full_coverage_is_judged_hit():
    """金标准两处、模型报出两处 → 命中。"""

    judgement = judge_case(
        GOLD_TWO_LESIONS,
        (
            _Candidate("右上叶", 5.0, "实性"),
            _Candidate("左下肺", 12.0, "磨玻璃"),
        ),
    )
    assert judgement.verdict == VERDICT_HIT
    assert judgement.coverage_state == COVERAGE_COVERED
    assert judgement.lesion_total == 2
    assert judgement.lesion_covered == 2
    assert judgement.uncovered_lesions == ()


def test_partial_coverage_is_judged_miss():
    """金标准两处、模型只报出一处 → **按最严口径判漏诊**。

    这是本任务的核心口径：只盖住最显眼的那一处不算命中 ——
    厂商只报最显眼的一处就能「通过」的话，最难的那处永远没人管。
    """

    judgement = judge_case(GOLD_TWO_LESIONS, (_Candidate("右上叶", 5.0, "实性"),))
    assert judgement.verdict == VERDICT_MISS
    assert judgement.coverage_state == COVERAGE_UNCOVERED
    assert judgement.lesion_total == 2
    assert judgement.lesion_covered == 1


def test_uncovered_labels_are_recorded():
    """没盖住的是**哪一处**必须被记下来（任务 30 的漏诊原因文案直接引用）。"""

    judgement = judge_case(GOLD_TWO_LESIONS, (_Candidate("右上叶", 5.0, "实性"),))

    assert len(judgement.uncovered_lesions) == 1
    entry = judgement.uncovered_lesions[0]
    assert entry.lesion.location == "左肺下叶"
    assert entry.lesion.size_mm == 12.0
    assert entry.lesion.density == "磨玻璃"

    text = judgement.uncovered_text()
    assert "左肺下叶" in text
    assert "12.0mm" in text
    assert "磨玻璃" in text


def test_verdict_reason_states_the_strict_criterion():
    """判定依据要说清「按最严口径判漏诊」与漏掉的是哪一处。"""

    judgement = judge_case(GOLD_TWO_LESIONS, (_Candidate("右上叶", 5.0, "实性"),))
    assert "最严口径" in judgement.reason
    assert "2 处病灶" in judgement.reason
    assert "左肺下叶" in judgement.reason


def test_one_candidate_cannot_cover_two_lesions():
    """**一个候选只能盖一处病灶** —— 金标准两处同部位，模型报一处 → 漏诊。"""

    gold = GoldLabels(
        presence=True,
        location="右肺上叶",
        size_mm=5.0,
        density="实性",
        lesions=(
            LesionLabels(location="右肺上叶", size_mm=5.0, density="实性"),
            LesionLabels(location="右肺上叶", size_mm=9.0, density="实性"),
        ),
    )
    judgement = judge_case(gold, (_Candidate("右上叶", 5.0, "实性"),))
    assert judgement.verdict == VERDICT_MISS
    assert judgement.lesion_covered == 1
    assert judgement.uncovered_lesions[0].lesion.size_mm == 9.0


def test_location_mismatch_leaves_the_lesion_uncovered():
    """部位对不上 = 不是同一处病灶 → 该处未覆盖 → 判漏诊。"""

    judgement = judge_case(
        GOLD_TWO_LESIONS,
        (
            _Candidate("右上叶", 5.0, "实性"),
            _Candidate("右上叶", 12.0, "磨玻璃"),
        ),
    )
    assert judgement.verdict == VERDICT_MISS
    assert judgement.uncovered_lesions[0].lesion.location == "左肺下叶"


def test_size_mismatch_does_not_refute_the_coverage():
    """**属性对不上 ≠ 不是那一处**：尺寸差 1mm（容差 0.5）仍算覆盖。

    把 8mm 量成 9mm 是**测量偏差**，不是「没看见」——
    判成漏诊会把敏感度算歪（同一处病灶被算了两次：一次测量差、一次没检出）。
    尺寸层的结论照样记进判定依据（复核的人能看到「尺寸不一致」）。
    """

    judgement = judge_case(
        GOLD_ONE_LESION,
        (_Candidate("右下肺", 9.0, "磨玻璃"),),
        size_tolerance_mm=0.5,
    )
    assert judgement.verdict == VERDICT_HIT
    assert judgement.coverage_state == COVERAGE_COVERED
    assert judgement.size_layer == LAYER_MISMATCH  # 偏差记下来，但不翻脸


def test_gold_without_attributes_is_covered_by_existence():
    """金标准只说「见结节」、没写部位尺寸密度 → 按存在性对应，**不算不可比**。

    「金标准没写」与「写法认不出」是两回事：
    前者没有可比的对象，但也没有任何东西否定它 —— 不该把这类例挡在分母外。
    """

    gold = GoldLabels(
        presence=True,
        lesions=(LesionLabels(location=None, size_mm=None, density=None),),
    )
    judgement = judge_case(gold, (_Candidate("右下肺", 8.0, "磨玻璃"),))
    assert judgement.verdict == VERDICT_HIT
    assert judgement.coverage_state == COVERAGE_COVERED
    assert judgement.has_incomparable() is False


def test_coverage_report_lists_every_pair():
    """逐对结论留着 —— 复核时能逐对核对「为什么这处算对应」。"""

    report = compare_coverage(
        GOLD_TWO_LESIONS,
        (
            _Candidate("右上叶", 5.0, "实性"),
            _Candidate("左下肺", 12.0, "磨玻璃"),
        ),
    )
    assert report.total == 2
    assert report.covered == 2
    assert report.fully_covered is True
    assert len(report.pairs) == 4  # 2 处病灶 × 2 个候选
    assert report.notes()


# --------------------------------------------------------------------------
# 2. 不可比与失败：各自单列，都不进 TP / FP / FN / TN
# --------------------------------------------------------------------------
def _row(**overrides) -> CaseRow:
    base = {
        "accession_no": "CASE-0001",
        "verdict": VERDICT_HIT,
    }
    base.update(overrides)
    return CaseRow(**base)


def test_incomparable_case_is_excluded_from_the_denominator():
    """属性不可比的例**不进分母** —— 当成一致是一次命中白送，当成不一致是一次漏诊。"""

    row = _row(incomparable=True, incomparable_reason="部位写法「肺门区」不在等价表里")
    assert counts_toward_denominator(row) is False

    partition = partition_rows([row])
    assert partition.counted == 0
    assert partition.incomparable == 1
    assert partition.failed == 0
    assert partition.needs_review == 0


def test_failed_case_is_excluded_from_the_denominator():
    """失败例照旧不进分母（任务 17 的红线，任务 28 不动它）。"""

    row = _row(verdict=VERDICT_FAILED, error_code=ErrorCode.INFERENCE_5XX.value)
    assert counts_toward_denominator(row) is False

    partition = partition_rows([row])
    assert partition.counted == 0
    assert partition.failed == 1
    assert partition.incomparable == 0


def test_incomparable_and_failed_do_not_mix_into_tp_fp_fn_tn():
    """一批例里同时有命中、不可比、失败 → 四项原始计数只算得住的。"""

    rows = [
        _row(accession_no="A", verdict=VERDICT_HIT),
        _row(accession_no="B", verdict=VERDICT_HIT, incomparable=True),
        _row(accession_no="C", verdict=VERDICT_FAILED),
        _row(accession_no="D", verdict=VERDICT_MISS),
    ]
    counts = count_metric_rows(rows)
    assert counts.tp == 1
    assert counts.fn == 1
    assert counts.fp == 0
    assert counts.tn == 0
    assert counts.counted == 2
    assert counts.incomparable == 1
    assert counts.failed == 1
    assert counts.needs_review == 0

    partition = partition_rows(rows)
    assert (partition.failed, partition.incomparable, partition.needs_review) == (
        counts.failed,
        counts.incomparable,
        counts.needs_review,
    )


def test_incomparable_does_not_become_a_miss():
    """不可比的例**不判漏诊** —— 对应关系判不出来就不下结论。"""

    gold = GoldLabels(
        presence=True,
        density="磨玻璃",
        lesions=(LesionLabels(location=None, size_mm=None, density="磨玻璃"),),
    )
    judgement = judge_case(gold, (_Candidate("密度不均", 8.0, "密度不均"),))

    assert judgement.has_incomparable() is True
    assert judgement.verdict != VERDICT_MISS
    assert judgement.coverage_state == COVERAGE_INCOMPARABLE
    assert judgement.incomparable_layers == ("density",)
    assert "不计入 TP/FP/FN/TN" in judgement.reason


def test_incomparable_layer_is_reported_with_the_offending_writing():
    """不可比要写明**哪一侧、哪个写法**认不出来。"""

    judgement = judge_case(
        GOLD_ONE_LESION,
        (_Candidate("肺门区", 8.0, "磨玻璃"),),
    )
    assert judgement.has_incomparable() is True
    assert judgement.incomparable_layers == ("location",)
    assert "肺门区" in judgement.incomparable_reason


# --------------------------------------------------------------------------
# 3. 口径写进产物：逐例行带上判定依据
# --------------------------------------------------------------------------
def test_judge_basis_carries_the_four_layers():
    """判定依据要能一眼看到四层结论（存在性 / 部位 / 尺寸 / 密度）。"""

    judgement = judge_case(
        GOLD_ONE_LESION,
        (_Candidate("右下肺", 8.0, "磨玻璃"),),
    )
    basis = judgement.basis_text()
    assert "存在性：一致" in basis
    assert "部位：一致" in basis
    assert "尺寸：一致" in basis
    assert "密度：一致" in basis
    assert "覆盖：" in basis


def _engine(tmp_path):
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


def test_engine_writes_judge_basis_and_uncovered_labels_into_case_rows(
    tmp_path, full_env, monkeypatch
):
    """端到端：金标准两处、模型报一处 → 逐例行带上判定依据与未覆盖标签。

    走真实出站链路（真 socket 替身 + 真 ``requests.Session``），
    产物落在 ``result.json`` 的 ``case_rows`` 里 ——
    复核的人不翻日志就能看到「这一例凭什么这么判」。
    """

    stub = InferenceStub(
        candidates=[{"position": "右上叶", "size_mm": 5.0, "density": "实性", "confidence": 0.9}]
    )
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        request = EvalRequest(
            run_id="strict-e2e",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="多标签最严口径端到端样例",
            cases=(
                CaseInput(
                    accession_no="STRICT-0001",
                    gold_standard_text=(
                        "右肺上叶见一实性结节，大小约 5mm；"
                        "左下肺见一磨玻璃结节，最大径约 12mm。"
                    ),
                    image_ref="dx://local/strict-0001",
                    study_uid="STUDY-STRICT-0001",
                    row_no=1,
                ),
            ),
        )
        response = engine.run(request)
    finally:
        stub.stop()

    row = response.case_rows[0]
    assert row.verdict == VERDICT_MISS
    assert row.uncovered_labels
    assert "左肺下叶" in row.uncovered_labels
    assert "存在性：一致" in row.judge_basis
    assert "覆盖：未覆盖" in row.judge_basis
    assert row.needs_review is False
    assert row.incomparable is False

    # 汇总行：这一例是一次真漏诊，进了分母
    assert response.metrics.fn == 1
    assert response.metrics.tp == 0
    assert response.metrics.incomparable_count == 0
    assert response.summary.evaluated_cases == 1


def test_engine_excludes_incomparable_case_from_the_metrics(tmp_path, full_env, monkeypatch):
    """端到端：厂商回了认不出的部位写法 → 该例**不进 TP/FP/FN/TN**，单列不可比。

    这一例既不是模型的错（它确实报出了结节），也不是金标准的错
    （放射科写的是我们表里没有的写法）—— 核不了就不算分，
    单列一栏让人去补等价表。
    """

    stub = InferenceStub(
        candidates=[{"position": "肺门区", "size_mm": 8.0, "density": "磨玻璃", "confidence": 0.9}]
    )
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        request = EvalRequest(
            run_id="strict-incomparable",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="不可比端到端样例",
            cases=(
                CaseInput(
                    accession_no="STRICT-0003",
                    gold_standard_text="右下肺见一磨玻璃结节，大小约 8mm。",
                    image_ref="dx://local/strict-0003",
                    study_uid="STUDY-STRICT-0003",
                    row_no=1,
                ),
            ),
        )
        response = engine.run(request)
    finally:
        stub.stop()

    row = response.case_rows[0]
    assert row.incomparable is True
    assert "肺门区" in row.incomparable_reason
    assert row.needs_review is True

    # **不进指标**：既不当命中也不当漏诊
    assert response.metrics.tp == 0
    assert response.metrics.fn == 0
    assert response.metrics.incomparable_count == 1
    assert response.summary.evaluated_cases == 0
    assert "不可比" in response.summary.degrade_note


def test_engine_writes_case_rows_into_result_json(tmp_path, full_env, monkeypatch):
    """``result.json`` 的 ``case_rows`` 里带着判定依据（表内插件据此回填）。"""

    stub = InferenceStub(
        candidates=[{"position": "右下肺", "size_mm": 8.0, "density": "磨玻璃", "confidence": 0.9}]
    )
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        request = EvalRequest(
            run_id="strict-basis",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="判定依据端到端样例",
            cases=(
                CaseInput(
                    accession_no="STRICT-0002",
                    gold_standard_text="右下肺见一磨玻璃结节，大小约 8mm。",
                    image_ref="dx://local/strict-0002",
                    study_uid="STUDY-STRICT-0002",
                    row_no=1,
                ),
            ),
            options=RunOptions(size_tolerance_mm=2.0),
        )
        engine.run(request)
    finally:
        stub.stop()

    payload = json.loads(
        (tmp_path / "runs" / "strict-basis" / "result.json").read_text(encoding="utf-8")
    )
    row = payload["case_rows"][0]
    assert row["verdict"] == VERDICT_HIT
    assert row["judge_basis"]
    assert "存在性" in row["judge_basis"]
    assert row["uncovered_labels"] == ""
    assert row["incomparable"] is False
