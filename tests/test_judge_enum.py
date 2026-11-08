"""判定引擎的 **5 值互斥完备枚举**（任务 26）。

这一组断言回答四个问题：

1. **枚举是完备的吗** —— 四值在已评估样本上是不是构成完整的 2×2
   （四个格子各落一个值，不重不漏）；
2. **枚举是互斥的吗** —— 任一输入组合是不是只落**一个**值，
   ``failed`` 是不是与四值**不相交**；
3. **失败优先吗** —— 调用失败 / 超时 / 标识不回显 / 响应缺字段，
   是不是一律先判 ``failed``，哪怕这次调用恰好也检出了东西；
4. **失败例进不进指标分母** —— 判了 ``failed`` 的例是不是被指标口径
   挡在分母外面（口径真源在 ``contracts.counts_toward_denominator``）。

**完备性不是靠注释保证的**：:func:`check_verdict_completeness` 会
**真的把判定跑一遍**（遍历 12 格输入空间），核对行为而不是核对声明 ——
声明改了行为没改，这里会立刻报出来。
"""

from __future__ import annotations

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    JUDGED_VERDICTS,
    VERDICT_FAILED,
    VERDICT_FALSE_POSITIVE,
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_TRUE_NEGATIVE,
    VERDICTS,
    CaseInput,
    CaseRow,
    EvalRequest,
    counts_toward_denominator,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError, error_text
from nodule_eval.goldstd import GoldLabels
from nodule_eval.metrics import (
    FAILURE_ERROR_CODES,
    LAYER_MATCH,
    LAYER_MISMATCH,
    LAYER_NOT_COMPARED,
    PRESENCE_MATRIX,
    VERDICT_DEFINITIONS,
    assert_verdict_completeness,
    check_verdict_completeness,
    enumerate_verdict_outcomes,
    judge_case,
)
from nodule_eval.store import RunStore

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"
#: 阴性金标准。
NEGATIVE_GOLD = "双肺未见明显结节影。"


def _candidates(count: int) -> tuple[object, ...]:
    """造若干候选（本层只看「有没有检出」，不看候选属性）。"""

    return tuple(
        type("Candidate", (), {"position": "右下肺", "size_mm": 8.0})() for _ in range(count)
    )


# --------------------------------------------------------------------------
# 1. 完备性与互斥性
# --------------------------------------------------------------------------
def test_five_verdicts_are_declared_and_all_reachable():
    """5 个判定值都登记了、都有中文名、且每个都真的能被判出来。"""

    assert set(VERDICT_DEFINITIONS) == set(VERDICTS)
    assert set(VERDICT_DEFINITIONS) == {
        VERDICT_HIT,
        VERDICT_MISS,
        VERDICT_FALSE_POSITIVE,
        VERDICT_TRUE_NEGATIVE,
        VERDICT_FAILED,
    }
    for code, definition in VERDICT_DEFINITIONS.items():
        assert definition.code == code
        assert definition.name_zh
        assert definition.condition

    reached = {outcome.verdict for outcome in enumerate_verdict_outcomes()}
    assert reached == set(VERDICTS)


def test_presence_matrix_is_a_complete_bijection():
    """存在性 2×2：四个格子各落一个四值判定，不重不漏。"""

    assert set(PRESENCE_MATRIX) == {
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    }
    assert set(PRESENCE_MATRIX.values()) == set(JUDGED_VERDICTS)
    assert len(set(PRESENCE_MATRIX.values())) == 4


def test_enumeration_covers_every_input_combination_without_overlap():
    """遍历 12 格输入空间：每格只落一个值，四值格互不重复，failed 与四值不相交。"""

    outcomes = enumerate_verdict_outcomes()
    # 输入空间 = {失败: 是/否} × {金标准存在性: 有/无/读不出} × {模型检出: 是/否}
    assert len(outcomes) == 12
    assert len({(o.failed, o.gold_presence, o.detected) for o in outcomes}) == 12

    four_value = [o for o in outcomes if not o.failed and o.gold_presence is not None]
    assert len(four_value) == 4
    assert len({o.verdict for o in four_value}) == 4
    assert set(o.verdict for o in four_value) == set(JUDGED_VERDICTS)

    # failed 只出现在「调用失败」或「金标准存在性读不出来」这两格里
    failed_cells = [o for o in outcomes if o.verdict == VERDICT_FAILED]
    assert failed_cells
    assert all(o.failed or o.gold_presence is None for o in failed_cells)
    # failed 与四值不相交
    assert VERDICT_FAILED not in {o.verdict for o in four_value}


def test_completeness_report_is_green():
    """完备性自检全绿 —— 这是任务 26 的验收口径。"""

    report = check_verdict_completeness()
    assert report.total_combinations == 12
    assert report.four_value_bijective is True
    assert report.mutually_exclusive is True
    assert report.failed_priority is True
    assert report.complete is True
    assert set(report.verdicts_covered) == set(VERDICTS)

    # 自检本身不抛
    assert assert_verdict_completeness().complete is True


def test_completeness_selfcheck_detects_a_broken_matrix(monkeypatch):
    """把真值表改坏（两格撞成同一个判定）→ 完备性自检立刻报出来。"""

    broken = dict(PRESENCE_MATRIX)
    broken[(False, True)] = VERDICT_HIT  # 误报格被改成命中
    monkeypatch.setattr("nodule_eval.metrics.judge.PRESENCE_MATRIX", broken)

    report = check_verdict_completeness()
    assert report.complete is False
    assert report.four_value_bijective is False

    with pytest.raises(NoduleEvalError) as excinfo:
        assert_verdict_completeness()
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID


# --------------------------------------------------------------------------
# 2. 四值判定本身
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("gold_presence", "candidate_count", "expected", "layer"),
    [
        (True, 1, VERDICT_HIT, LAYER_MATCH),
        (True, 2, VERDICT_HIT, LAYER_MATCH),
        (True, 0, VERDICT_MISS, LAYER_MISMATCH),
        (False, 1, VERDICT_FALSE_POSITIVE, LAYER_MISMATCH),
        (False, 2, VERDICT_FALSE_POSITIVE, LAYER_MISMATCH),
        (False, 0, VERDICT_TRUE_NEGATIVE, LAYER_MATCH),
    ],
)
def test_presence_layer_decides_the_four_value_verdict(
    gold_presence, candidate_count, expected, layer
):
    """存在性精确比对定案：四个格子各落各的判定。"""

    judgement = judge_case(
        GoldLabels(presence=gold_presence),
        _candidates(candidate_count),
    )
    assert judgement.verdict == expected
    assert judgement.presence_layer == layer
    assert judgement.gold_presence is gold_presence
    assert judgement.detected is (candidate_count > 0)
    assert judgement.candidate_count == candidate_count
    assert judgement.error_code == ""
    # 属性三层本轮还没比 —— 记「未比对」，不假装比过了
    assert judgement.size_layer == LAYER_NOT_COMPARED
    assert judgement.location_layer == LAYER_NOT_COMPARED
    assert judgement.density_layer == LAYER_NOT_COMPARED
    assert judgement.layers()["presence"] == layer


def test_judgement_carries_readable_reason_and_serialisable_basis():
    """判定依据要说清「凭什么这么判」，且能落进产物。"""

    judgement = judge_case(GoldLabels(presence=True), _candidates(0))
    assert judgement.verdict == VERDICT_MISS
    assert "漏诊" in judgement.reason
    assert "金标准存在性=有病灶" in judgement.reason

    payload = judgement.to_dict()
    assert payload["verdict"] == VERDICT_MISS
    assert payload["layers"]["presence"] == LAYER_MISMATCH
    assert payload["gold_presence"] is True
    assert payload["detected"] is False


# --------------------------------------------------------------------------
# 3. 失败优先
# --------------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(FAILURE_ERROR_CODES))
def test_every_call_failure_code_judges_failed(code):
    """调用类错误码（失败 / 超时 / 标识不回显 / 响应缺字段）一律判 failed。"""

    judgement = judge_case(
        GoldLabels(presence=True),
        _candidates(1),
        error=error_text(code),
        error_code=code,
    )
    assert judgement.verdict == VERDICT_FAILED
    assert judgement.error_code == code
    assert judgement.presence_layer == LAYER_NOT_COMPARED


@pytest.mark.parametrize(
    ("gold_presence", "candidate_count"),
    [(True, 0), (True, 1), (False, 0), (False, 1)],
)
def test_failed_takes_priority_over_any_four_value_verdict(gold_presence, candidate_count):
    """失败优先：哪怕这次调用恰好检出了东西、哪怕存在性其实一致，也先判 failed。"""

    without_failure = judge_case(
        GoldLabels(presence=gold_presence), _candidates(candidate_count)
    )
    assert without_failure.verdict in JUDGED_VERDICTS

    with_failure = judge_case(
        GoldLabels(presence=gold_presence),
        _candidates(candidate_count),
        error="推理服务暂时不可用（HTTP 500）：服务端错误",
        error_code=ErrorCode.INFERENCE_5XX.value,
    )
    assert with_failure.verdict == VERDICT_FAILED
    assert with_failure.error_code == ErrorCode.INFERENCE_5XX.value
    assert with_failure.reason == "推理服务暂时不可用（HTTP 500）：服务端错误"


def test_failure_reason_comes_from_the_error_code_table():
    """只给错误码不给原因 → 原因从统一错误码表取（业务代码零硬编码文案）。"""

    judgement = judge_case(
        GoldLabels(presence=False),
        _candidates(0),
        error_code=ErrorCode.INFERENCE_TIMEOUT.value,
    )
    assert judgement.verdict == VERDICT_FAILED
    assert judgement.reason == error_text(ErrorCode.INFERENCE_TIMEOUT.value)
    assert "超时" in judgement.reason


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.MODEL_UNAVAILABLE.value,
        ErrorCode.MODEL_REQUEST_REJECTED.value,
        ErrorCode.RULE_DEGRADED.value,
    ],
)
def test_degraded_codes_are_not_treated_as_case_failures(code):
    """降级码不是「这一例没拿到结论」—— 降级例有结论，仍走四值判定。"""

    judgement = judge_case(
        GoldLabels(presence=True),
        _candidates(1),
        error_code=code,
    )
    assert judgement.verdict == VERDICT_HIT
    assert judgement.error_code == ""


def test_unreadable_gold_presence_is_failed_not_negative():
    """金标准存在性读不出来 → 判 failed（挂人工复核），绝不落成阴性。"""

    judgement = judge_case(GoldLabels(presence=None), _candidates(0))
    assert judgement.verdict == VERDICT_FAILED
    assert judgement.error_code == ErrorCode.RULE_DEGRADED.value
    assert "存在性" in judgement.reason
    assert judgement.presence_layer == LAYER_NOT_COMPARED


def test_failure_wins_over_unreadable_gold_presence():
    """既失败又读不出存在性 → 仍然是 failed，且错误码是失败的那个。"""

    judgement = judge_case(
        GoldLabels(presence=None),
        _candidates(0),
        error="推理服务暂时不可用（HTTP 500）：服务端错误",
        error_code=ErrorCode.INFERENCE_5XX.value,
    )
    assert judgement.verdict == VERDICT_FAILED
    assert judgement.error_code == ErrorCode.INFERENCE_5XX.value


# --------------------------------------------------------------------------
# 4. 与指标口径的衔接
# --------------------------------------------------------------------------
def test_failed_verdict_never_counts_toward_the_denominator():
    """判 failed 的例不进指标分母；四值例进分母（口径真源在 contracts）。"""

    failed = CaseRow(accession_no="A-1", verdict=VERDICT_FAILED)
    assert counts_toward_denominator(failed) is False

    for verdict in JUDGED_VERDICTS:
        row = CaseRow(accession_no="A-2", verdict=verdict)
        assert counts_toward_denominator(row) is True


# --------------------------------------------------------------------------
# 5. 端到端：判定在真实出站链路上落 5 值之一
# --------------------------------------------------------------------------
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


def _request(cases, *, run_id: str) -> EvalRequest:
    return EvalRequest(
        run_id=run_id,
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref="判定枚举端到端样例",
        cases=tuple(cases),
    )


def test_engine_verdicts_land_in_the_five_value_enum(tmp_path, full_env, monkeypatch):
    """真实出站链路（真 socket 替身）：逐例判定落在 5 值之内，且四值格各归各位。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        cases = (
            CaseInput(
                accession_no="JUDGE-0001",
                gold_standard_text=POSITIVE_GOLD,
                image_ref="dx://local/0001",
                study_uid="STUDY-0001",
                row_no=1,
            ),
            CaseInput(
                accession_no="JUDGE-0002",
                gold_standard_text=NEGATIVE_GOLD,
                image_ref="dx://local/0002",
                study_uid="STUDY-0002",
                row_no=2,
            ),
        )
        response = engine.run(_request(cases, run_id="judge-enum"))
    finally:
        stub.stop()

    verdicts = [row.verdict for row in response.case_rows]
    assert set(verdicts) <= set(VERDICTS)
    # 替身每例都回一个候选：阳性例命中、阴性例误报
    assert verdicts == [VERDICT_HIT, VERDICT_FALSE_POSITIVE]
    # 判定依据逐层落进逐例产物
    artifact = tmp_path / "runs" / "judge-enum" / "cases" / "JUDGE-0001" / "attempt-0001" / "case.json"
    import json

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    layers = payload["judgement"]["layers"]
    assert layers["presence"] == LAYER_MATCH
    # **任务 27 起**：存在性一致时属性三层也会给出结论 ——
    # 替身回的候选（右下肺 / 8.0mm / 磨玻璃）与金标准完全对得上，三层都判一致。
    assert layers["size"] == LAYER_MATCH
    assert layers["location"] == LAYER_MATCH
    assert layers["density"] == LAYER_MATCH
    assert payload["judgement"]["incomparable_layers"] == []
