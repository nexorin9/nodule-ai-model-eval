"""逐例 fan-out 调度主循环：选定模型 → 调用 → 归并 → 判定 → 下一例。

这一组断言回答四个问题：

1. **fan-out 真的在跑吗** —— 两百例规模下总调用次数 = 病例数、每例恰一条
   ``tool_call``、判定结果与 ``case_rows`` 逐行一致；
2. **结论能反查回原始调用吗** —— ``case_rows.csv`` 的 ``tool_call_id``
   能在 ``tool_calls.jsonl`` 命中，且指向的是同一例；
3. **重跑会不会把上一版抹掉** —— 逐例产物按 attempt 序号另起一份，历史留着；
4. **循环什么时候停** —— 病例集跑完就停；单例失败不终止整批；
   run 级致命错误才把整轮标成 ``failed``。

**断言走的是真实出站**：替身是真监听 socket 的 HTTP 服务，
产品的 ``requests.Session`` / URL 拼接 / Bearer 头 / 请求体一行都没改。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.audit.toolcall import index_tool_calls, join_case_rows
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    VERDICT_FAILED,
    VERDICT_FALSE_POSITIVE,
    VERDICT_HIT,
    VERDICT_TRUE_NEGATIVE,
    CaseInput,
    EvalRequest,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.store import (
    CASE_STATUS_JUDGED,
    RUN_STATUS_EVALUATED,
    RUN_STATUS_FAILED,
    RunStore,
)

#: 阳性金标准（放射科写法：部位 + 密度 + 尺寸）。
POSITIVE_GOLD = "右下肺见一磨玻璃结节，大小约 8mm。"
#: 阴性金标准。
NEGATIVE_GOLD = "双肺未见明显结节影。"
#: 读不出存在性的金标准（既没说有、也没说没有）。
UNKNOWN_GOLD = "胸部正位片，摄片位置满意。"

#: fan-out 的规模（任务 14 的验收规模）。
SCALE = 200


def _cases(count: int, gold: str, *, prefix: str = "ACC") -> tuple[CaseInput, ...]:
    """造一批病例（每例自带影像标识，不必回读 RIS）。"""

    return tuple(
        CaseInput(
            accession_no=f"{prefix}-{index:04d}",
            gold_standard_text=gold,
            image_ref=f"dx://local/{index:04d}",
            study_uid=f"STUDY-{index:04d}",
            row_no=index + 1,
        )
        for index in range(count)
    )


def _engine(tmp_path: Path) -> Engine:
    """造一个挂上 run store 的引擎（退避不真睡，测试才跑得快）。"""

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


def _request(cases: Sequence[CaseInput], *, run_id: str) -> EvalRequest:
    return EvalRequest(
        run_id=run_id,
        model_id="demo-model",
        model_version="v1.0.0",
        dataset_ref="本轮 fan-out 验收样例",
        cases=tuple(cases),
    )


def _ledger(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "tool_calls.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_two_hundred_cases_fan_out_one_call_each(tmp_path, full_env, monkeypatch):
    """两百例规模：总调用次数 = 病例数，每例恰一条底账，判定与逐例行一致。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(SCALE, POSITIVE_GOLD), run_id="fanout-200"))
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()

    # ---- 总调用次数 = 病例数（一例一次，不重调、不漏调）----
    assert len(stub.requests) == SCALE
    assert len(response.case_rows) == SCALE

    # ---- 每例恰一条 tool_call，且 id 全局唯一 ----
    ledger = _ledger(tmp_path / "runs" / "fanout-200")
    assert len(ledger) == SCALE
    call_ids = [record["tool_call_id"] for record in ledger]
    assert len(set(call_ids)) == SCALE, "调用 id 必须逐例唯一"

    row_call_ids = [row.tool_call_id for row in response.case_rows]
    assert len(set(row_call_ids)) == SCALE
    assert set(row_call_ids) == set(call_ids)

    # ---- 判定结果与 case_rows 一致（金标准说有、替身回一个候选 → 命中）----
    assert {row.verdict for row in response.case_rows} == {VERDICT_HIT}
    assert response.summary.evaluated_cases == SCALE
    assert response.summary.failed_count == 0
    assert response.summary.needs_review_count == 0

    # ---- 库里的逐例行与产物对得上 ----
    assert store.count_cases("fanout-200") == SCALE
    recomputed = store.recompute("fanout-200")
    assert [row.verdict for row in recomputed] == [row.verdict for row in response.case_rows]
    store.close()


def test_case_rows_join_to_the_call_ledger(tmp_path, full_env, monkeypatch):
    """逐例行按 tool_call_id 关联调用底账 —— 结论能反查回原始调用。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(SCALE, POSITIVE_GOLD), run_id="fanout-join"))
    finally:
        stub.stop()

    ledger = _ledger(tmp_path / "runs" / "fanout-join")
    index = index_tool_calls(ledger)
    assert len(index) == SCALE

    joined = join_case_rows(response.case_rows, ledger)
    assert len(joined) == SCALE
    for item in joined:
        assert item.joined, "每一行都要在底账里找到那次调用"
        assert item.tool_call is not None
        # 底账里的入参指向的是**同一例**
        assert item.tool_call["args"]["accession_no"] == item.row.accession_no
        assert item.tool_call["name"] == "demo-model.detect"


def test_negative_gold_against_detected_candidate_is_false_positive(
    tmp_path, full_env, monkeypatch
):
    """金标准说没有、模型检出候选 → 误报（存在性层不一致）。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(20, NEGATIVE_GOLD), run_id="fanout-fp"))
    finally:
        stub.stop()

    assert {row.verdict for row in response.case_rows} == {VERDICT_FALSE_POSITIVE}


def test_negative_gold_against_empty_detection_is_true_negative(
    tmp_path, full_env, monkeypatch
):
    """金标准说没有、模型也没检出 → 真阴性。"""

    stub = InferenceStub(candidates=[])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(20, NEGATIVE_GOLD), run_id="fanout-tn"))
    finally:
        stub.stop()

    assert {row.verdict for row in response.case_rows} == {VERDICT_TRUE_NEGATIVE}
    assert response.summary.evaluated_cases == 20


def test_per_case_artifacts_are_archived_and_rerun_keeps_history(
    tmp_path, full_env, monkeypatch
):
    """逐例产物按 attempt 另起一份：重跑不覆盖历史。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        engine.run(_request(_cases(3, POSITIVE_GOLD), run_id="fanout-artifact"))
        first_dir = tmp_path / "runs" / "fanout-artifact" / "cases" / "ACC-0000"
        first = sorted(p.name for p in first_dir.iterdir())
        assert first == ["attempt-0001"]

        first_artifact = json.loads(
            (first_dir / "attempt-0001" / "case.json").read_text(encoding="utf-8")
        )
        assert first_artifact["accession_no"] == "ACC-0000"
        assert first_artifact["verdict"] == VERDICT_HIT
        assert first_artifact["tool_call_id"].startswith("tc_")
        assert first_artifact["judgement"]["layers"]["presence"] == "一致"
        assert first_artifact["gold_labels"]["presence"] is True

        # 同一 run_id 重跑 → 另起 attempt-0002，第一版一字未动
        engine.run(_request(_cases(3, POSITIVE_GOLD), run_id="fanout-artifact"))
        versions = sorted(p.name for p in first_dir.iterdir())
        assert versions == ["attempt-0001", "attempt-0002"]

        again = json.loads(
            (first_dir / "attempt-0002" / "case.json").read_text(encoding="utf-8")
        )
        assert again["attempt"] == 2
        assert again["tool_call_id"] != first_artifact["tool_call_id"]
        assert json.loads(
            (first_dir / "attempt-0001" / "case.json").read_text(encoding="utf-8")
        ) == first_artifact
    finally:
        stub.stop()


def test_case_artifact_keeps_the_gold_standard_hash_not_the_text(
    tmp_path, full_env, monkeypatch
):
    """逐例产物里**不落金标准原文**（可能带患者可识别信息），只落哈希。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        engine.run(_request(_cases(1, POSITIVE_GOLD), run_id="fanout-hash"))
    finally:
        stub.stop()

    artifact = (
        tmp_path
        / "runs"
        / "fanout-hash"
        / "cases"
        / "ACC-0000"
        / "attempt-0001"
        / "case.json"
    )
    text = artifact.read_text(encoding="utf-8")
    assert POSITIVE_GOLD not in text, "原文不进产物"
    payload = json.loads(text)
    assert len(payload["gold_standard_sha256"]) == 64


def test_run_and_case_states_reach_evaluated_and_judged(
    tmp_path, full_env, monkeypatch
):
    """循环退出后 run 推进到 evaluated，每一例推进到 judged。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        engine.run(_request(_cases(5, POSITIVE_GOLD), run_id="fanout-state"))
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()

    assert store.get_run("fanout-state").status == RUN_STATUS_EVALUATED
    assert store.get_case("fanout-state", "ACC-0000").status == CASE_STATUS_JUDGED

    run_moves = [(m.from_state, m.to_state) for m in store.list_transitions("fanout-state", scope="run")]
    assert run_moves == [
        ("draft", "assembled"),
        ("assembled", "running"),
        ("running", "called"),
        ("called", "evaluated"),
    ]
    case_moves = [
        (m.from_state, m.to_state)
        for m in store.list_transitions("fanout-state", scope="case", subject="ACC-0000")
    ]
    assert case_moves == [
        ("pending", "called"),
        ("called", "normalized"),
        ("normalized", "judged"),
    ]
    store.close()


def test_one_failing_case_does_not_stop_the_batch(tmp_path, full_env, monkeypatch):
    """单例失败不终止循环：其余例照跑，整批不被一行拖垮。

    **失败按检查号注入**（``fail_study_uids``）而不是按「第几次请求」——
    引擎并发跑时第几次请求落在哪一例上是不确定的；按请求顺序注入的话，
    失败会落到不同的例上，这条断言每次跑结果都不一样。
    """

    # 第一例的检查影像标识命中失败名单；其余例一律成功
    stub = InferenceStub(fail_study_uids=["STUDY-0000"])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(20, POSITIVE_GOLD), run_id="fanout-partial"))
    finally:
        stub.stop()

    assert len(response.case_rows) == 20
    verdicts = [row.verdict for row in response.case_rows]
    assert verdicts[0] == VERDICT_FAILED
    assert set(verdicts[1:]) == {VERDICT_HIT}
    assert response.summary.failed_count == 1
    assert response.summary.evaluated_cases == 19

    # 失败例同样留底账（凭什么算失败要能反查）
    ledger = _ledger(tmp_path / "runs" / "fanout-partial")
    failed_row = response.case_rows[0]
    index = index_tool_calls(ledger)
    assert failed_row.tool_call_id in index
    assert "INFERENCE_5XX" in index[failed_row.tool_call_id]["content"]
    assert failed_row.error_code == ErrorCode.INFERENCE_5XX.value


def test_run_level_fatal_error_marks_the_run_failed(tmp_path, full_env, monkeypatch):
    """run 级致命错误：整轮标 failed 并抛出，不留半截「跑完了」的产物。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)

        def _boom(_case: CaseInput):
            raise RuntimeError("存储层不可用")

        engine.normalizer = _boom  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            engine.run(_request(_cases(3, POSITIVE_GOLD), run_id="fanout-fatal"))
        store = engine.test_store  # type: ignore[attr-defined]
    finally:
        stub.stop()

    assert store.get_run("fanout-fatal").status == RUN_STATUS_FAILED
    reasons = [m.reason for m in store.list_transitions("fanout-fatal", scope="run")]
    assert any("整轮失败" in reason for reason in reasons)
    store.close()


def test_unreadable_gold_standard_is_held_for_review(tmp_path, full_env, monkeypatch):
    """金标准读不出存在性 → 挂人工复核，不进指标分母，绝不当阴性。"""

    stub = InferenceStub()
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(3, UNKNOWN_GOLD), run_id="fanout-unknown"))
    finally:
        stub.stop()

    row = response.case_rows[0]
    assert row.verdict == VERDICT_FAILED
    assert row.needs_review is True
    assert row.error_code == ErrorCode.RULE_DEGRADED.value
    assert response.summary.needs_review_count == 3
    assert response.summary.evaluated_cases == 0
    assert response.summary.rule_degraded_count == 3


def test_fanout_never_silently_turns_a_failure_into_a_negative(
    tmp_path, full_env, monkeypatch
):
    """红线：任何一类失败都判 failed，绝不落成「未检出」。"""

    stub = InferenceStub(status_sequence=[500, 500, 500])
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        response = engine.run(_request(_cases(2, NEGATIVE_GOLD), run_id="fanout-redline"))
    finally:
        stub.stop()

    assert {row.verdict for row in response.case_rows} == {VERDICT_FAILED}
    assert response.summary.evaluated_cases == 0
